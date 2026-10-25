"""
Backtest strategii MAC (przecięcie średnich MA50 / MA200) na indeksie sWIG80.

Dane:
  * sWIG80 - oficjalne notowania z GPW Benchmark (gpwbenchmark.pl), historia od 1994 r.
  * Wyniki strategii i porównanie z Buy & Hold trafiają do output/sWIG80/.

Zasady:
  * KUPNO  - gdy MA50 przecina MA200 od dołu (golden cross: MA50[t-1] <= MA200[t-1] i MA50[t] > MA200[t]).
  * SPRZEDAŻ - gdy MA50 przecina MA200 od góry (death cross: MA50[t-1] >= MA200[t-1] i MA50[t] < MA200[t]).
  * Okresy średnich można zmienić parametrami --fast i --slow.
  * Transakcja realizowana po cenie zamknięcia sesji sygnałowej (zlecenie MOC) lub,
    opcjonalnie, z opóźnieniem o N sesji (--delay).
  * Po każdej transakcji (kupno lub sprzedaż) kolejne transakcje są zablokowane przez N sesji
    (--cooldown, domyślnie 12). Jeśli w trakcie blokady pojawi się przeciwny sygnał, to po jej
    zakończeniu pozycja jest wyrównywana do bieżącego położenia MA50 względem MA200.
  * --optimize [MAX] przeszukuje cooldown 0..MAX i uruchamia backtest z wartością maksymalizującą CAGR.
  * Poza rynkiem kapitał leży w gotówce (opcjonalne oprocentowanie --cash-rate).
  * Koszty transakcyjne pobierane przy każdym zleceniu (kupno i sprzedaż):
    prowizja maklerska (% wartości, z kwotą minimalną) + połowa spreadu bid/ask.
    Domyślne, typowe wartości dla sWIG80 są w Config; można je nadpisać
    opcjami --commission, --min-commission, --spread. Buy & Hold płaci jednorazowy koszt kupna.

Użycie:
    python swig80_backtest.py
    python swig80_backtest.py --optimize 60
    python swig80_backtest.py --commission 0.0039 --min-commission 5 --spread 0.006
    python swig80_backtest.py --years 50 --fast 50 --slow 200 --cash-rate 0.0 --delay 0 --cooldown 12
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass, replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from curl_cffi import requests as creq

TRADING_DAYS = 252

SWIG80_ISIN = "PL9999999979"


@dataclass
class Config:
    years: int = 22
    fast: int = 50  # krótka średnia ruchoma [sesje]
    slow: int = 200  # długa średnia ruchoma [sesje]
    commission: float = 0.0029  # prowizja na stronę transakcji (0.29%)
    min_commission: float = 5.0  # minimalna prowizja [PLN]
    spread: float = 0.0040  # pełny spread bid/ask (każda strona płaci połowę)
    cash_rate: float = 0.0  # roczne oprocentowanie gotówki poza rynkiem
    delay: int = 0  # 0 = transakcja na zamknięciu sesji sygnałowej, 1 = na zamknięciu kolejnej sesji
    # Blokada nowych transakcji po każdej transakcji [sesje]. 12 dobrano dla starej strategii
    # cena/SMA200; przy MAC 50/200 przecięcia są rzadkie i blokada praktycznie nie działa (zob. --optimize).
    cooldown: int = 12
    initial_capital: float = 10_000.0
    out_dir: Path = Path("output") / "sWIG80"

    @property
    def name(self) -> str:
        return "sWIG80"


# --------------------------------------------------------------------------- #
# Dane
# --------------------------------------------------------------------------- #
def fetch_gpw_index() -> pd.Series:
    """Pełna historia dzienna (zamknięcia) sWIG80 z GPW Benchmark."""
    import json

    # GPW Benchmark wymaga klienta z sygnaturą przeglądarki.
    with creq.Session(impersonate="chrome") as session:
        page = session.get(f"https://gpwbenchmark.pl/karta-indeksu?isin={SWIG80_ISIN}", timeout=30)
        page.raise_for_status()
        resp = session.get(
            "https://gpwbenchmark.pl/chart-json.php",
            params={"req": json.dumps([{"isin": SWIG80_ISIN, "mode": "ARCH"}])},
            timeout=120,
        )
        resp.raise_for_status()
        payload = resp.json()
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError("GPW Benchmark zwrócił nieprawidłowy format odpowiedzi dla sWIG80.")
    rows = payload[0].get("data")
    if not rows:
        raise RuntimeError("GPW Benchmark nie zwrócił danych dla sWIG80.")
    data = pd.DataFrame(rows)
    if not {"t", "c"}.issubset(data.columns):
        raise RuntimeError("W danych GPW Benchmark brakuje dat lub cen zamknięcia.")
    idx = pd.to_datetime(data["t"], unit="s", utc=True).dt.tz_convert("Europe/Warsaw").dt.tz_localize(None).dt.normalize()
    close = pd.Series(data["c"].astype(float).to_numpy(), index=pd.DatetimeIndex(idx), name="Close")
    if close.index.hasnans or not np.isfinite(close.to_numpy()).all() or (close <= 0).any():
        raise RuntimeError("Dane GPW Benchmark zawierają nieprawidłowe daty lub ceny.")
    return close[~close.index.duplicated(keep="last")].sort_index()


def download_data(cfg: Config) -> pd.DataFrame:
    end = pd.Timestamp.now(tz="Europe/Warsaw").tz_localize(None).normalize() + pd.Timedelta(days=1)
    start = end - pd.DateOffset(years=cfg.years)
    # Dodatkowy bufor, aby obie średnie były policzone już od pierwszego dnia okresu testu
    warmup_start = start - pd.DateOffset(days=int(max(cfg.fast, cfg.slow) * 1.6) + 30)

    print(f"\nPobieranie {cfg.name} (GPW Benchmark) od {warmup_start.date()} do {end.date()} ...")
    close = fetch_gpw_index()
    close = close.loc[(close.index >= warmup_start) & (close.index < end)]

    df = close.to_frame("Close")
    df["MA_fast"] = df["Close"].rolling(cfg.fast).mean()
    df["MA_slow"] = df["Close"].rolling(cfg.slow).mean()
    df = df.loc[df.index >= start].dropna()
    if len(df) < 2:
        raise RuntimeError(f"Za mało danych dla {cfg.name} w zadanym okresie.")
    if df.index[0] - start > pd.Timedelta(days=30):
        print(f"UWAGA: historia {cfg.name} jest krótsza niż {cfg.years} lat (dane od {close.index[0].date()}).")
    print(f"Okres testu {cfg.name}: {df.index[0].date()} - {df.index[-1].date()} ({len(df)} sesji)")
    return df


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #
def run_backtest(df: pd.DataFrame, cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.copy()
    above = df["MA_fast"] > df["MA_slow"]
    prev_above = above.shift(1, fill_value=above.iloc[0])

    df["cross_up"] = above & ~prev_above
    df["cross_down"] = ~above & prev_above

    # Pozycja docelowa: 1 po sygnale kupna, 0 po sygnale sprzedaży.
    # Startujemy w gotówce - pierwsze wejście dopiero po pierwszym golden crossie.
    signal = pd.Series(np.nan, index=df.index)
    signal[df["cross_up"]] = 1.0
    signal[df["cross_down"]] = 0.0
    signal.iloc[0] = 0.0
    df["target"] = signal.ffill()

    df["signal_pos"], df["blocked"] = apply_cooldown(df["target"].to_numpy(), cfg.cooldown, df.index)

    # Pozycja obowiązująca na zamknięciu dnia t (po ew. transakcji), z opcjonalnym opóźnieniem
    df["position"] = df["signal_pos"].shift(cfg.delay, fill_value=0.0)
    # Zwrot w dniu t przypada pozycji trzymanej od zamknięcia t-1
    held = df["position"].shift(1, fill_value=0.0)

    mkt_ret = df["Close"].pct_change().fillna(0.0)
    cash_daily = (1 + cfg.cash_rate) ** (1 / TRADING_DAYS) - 1
    trades_flag = df["position"].diff().abs().fillna(df["position"].iloc[0])

    gross_ret = held * mkt_ret + (1 - held) * cash_daily
    df["equity"], df["cost"], df["cost_frac"] = apply_costs(gross_ret, trades_flag, cfg)
    df["strat_ret"] = df["equity"].pct_change().fillna(df["equity"].iloc[0] / cfg.initial_capital - 1)

    # Buy & Hold płaci jednorazowo koszt kupna na starcie
    bh_entry = np.zeros(len(df))
    bh_entry[0] = 1.0
    df["bh_equity"], _, _ = apply_costs(mkt_ret, pd.Series(bh_entry, index=df.index), cfg)
    df["bh_ret"] = df["bh_equity"].pct_change().fillna(df["bh_equity"].iloc[0] / cfg.initial_capital - 1)

    trades = extract_trades(df, cfg)
    return df, trades


def transaction_cost_frac(value: float, cfg: Config) -> float:
    """Koszt jednej strony transakcji jako ułamek jej wartości: prowizja (z minimum) + połowa spreadu."""
    if value <= 0:
        return 0.0
    commission = max(cfg.commission * value, cfg.min_commission)
    return min(commission / value + cfg.spread / 2, 1.0)


def apply_costs(gross_ret: pd.Series, trades_flag: pd.Series, cfg: Config) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Krzywa kapitału po kosztach transakcyjnych.

    Transakcja odbywa się na zamknięciu sesji, więc koszt pobierany jest od wartości portfela
    z tego zamknięcia. Ze względu na minimalną prowizję liczony jest sekwencyjnie.
    Zwraca: kapitał, koszt w walucie i koszt jako ułamek wartości transakcji.
    """
    growth = (1 + gross_ret).cumprod().to_numpy()
    flags = trades_flag.to_numpy()
    cost_frac = np.zeros(len(growth))
    cost = np.zeros(len(growth))
    factor = np.ones(len(growth))
    f = 1.0
    for i in range(len(growth)):
        if flags[i] > 0:
            value = cfg.initial_capital * growth[i] * f
            cost_frac[i] = transaction_cost_frac(value, cfg)
            cost[i] = value * cost_frac[i]
            f *= 1 - cost_frac[i]
        factor[i] = f
    idx = gross_ret.index
    equity = pd.Series(cfg.initial_capital * growth * factor, index=idx)
    return equity, pd.Series(cost, index=idx), pd.Series(cost_frac, index=idx)


def apply_cooldown(target: np.ndarray, cooldown: int, index: pd.Index) -> tuple[pd.Series, pd.Series]:
    """Po każdej transakcji blokuje kolejne przez `cooldown` sesji.

    Sygnał, który wypadnie w okresie blokady, nie przepada: w pierwszej sesji po blokadzie
    pozycja jest wyrównywana do bieżącego stanu (MA szybka powyżej / poniżej MA wolnej).
    Zwraca pozycję oraz flagę sesji, w których transakcja była wstrzymana przez blokadę.
    """
    position = np.zeros(len(target))
    blocked = np.zeros(len(target), dtype=bool)
    pos = 0.0
    last_trade = -(10**9)
    for i, desired in enumerate(target):
        if desired != pos:
            if i - last_trade > cooldown:
                pos = desired
                last_trade = i
            else:
                blocked[i] = True
        position[i] = pos
    return pd.Series(position, index=index), pd.Series(blocked, index=index)


def extract_trades(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    pos = df["position"]
    change = pos.diff().fillna(pos.iloc[0])
    entries = df.index[change > 0]
    exits = df.index[change < 0]

    rows = []
    for entry in entries:
        later_exits = exits[exits > entry]
        is_open = len(later_exits) == 0
        exit_ = df.index[-1] if is_open else later_exits[0]

        entry_px = df.at[entry, "Close"]
        exit_px = df.at[exit_, "Close"]
        gross = exit_px / entry_px - 1
        cf_entry = df.at[entry, "cost_frac"]
        cf_exit = 0.0 if is_open else df.at[exit_, "cost_frac"]
        net = (1 + gross) * (1 - cf_entry) * (1 - cf_exit) - 1
        trade_cost = df.at[entry, "cost"] + (0.0 if is_open else df.at[exit_, "cost"])

        window = df.loc[entry:exit_, "Close"]
        mae = window.min() / entry_px - 1  # maksymalna niekorzystna zmiana w trakcie transakcji
        mfe = window.max() / entry_px - 1  # maksymalna korzystna zmiana w trakcie transakcji

        rows.append(
            {
                "entry_date": entry,
                "exit_date": exit_,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "return_pct": net * 100,
                "gross_return_pct": gross * 100,
                "costs": trade_cost,
                "sessions": df.index.get_loc(exit_) - df.index.get_loc(entry),
                "days": (exit_ - entry).days,
                "MAE_pct": mae * 100,
                "MFE_pct": mfe * 100,
                "open": is_open,
            }
        )
    return pd.DataFrame(rows, columns=[
        "entry_date", "exit_date", "entry_price", "exit_price", "return_pct",
        "gross_return_pct", "costs", "sessions", "days", "MAE_pct", "MFE_pct", "open",
    ]).astype({"open": bool})


# --------------------------------------------------------------------------- #
# Statystyki
# --------------------------------------------------------------------------- #
def max_drawdown(equity: pd.Series) -> tuple[float, pd.Timestamp, pd.Timestamp, pd.Timestamp | None]:
    peak = equity.cummax()
    dd = equity / peak - 1
    trough = dd.idxmin()
    peak_date = equity.loc[:trough].idxmax()
    recovered = equity.loc[trough:][equity.loc[trough:] >= equity.loc[peak_date]]
    recovery_date = recovered.index[0] if len(recovered) else None
    return dd.min(), peak_date, trough, recovery_date


def performance_stats(returns: pd.Series, equity: pd.Series, cfg: Config) -> dict:
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / cfg.initial_capital - 1
    cagr = (equity.iloc[-1] / cfg.initial_capital) ** (1 / years) - 1
    vol = returns.std() * np.sqrt(TRADING_DAYS)
    rf_daily = (1 + cfg.cash_rate) ** (1 / TRADING_DAYS) - 1
    excess = returns - rf_daily
    sharpe = excess.mean() / returns.std() * np.sqrt(TRADING_DAYS) if returns.std() > 0 else np.nan
    downside = returns[returns < 0].std() * np.sqrt(TRADING_DAYS)
    sortino = excess.mean() * TRADING_DAYS / downside if downside > 0 else np.nan
    mdd, peak, trough, rec = max_drawdown(equity)
    calmar = cagr / abs(mdd) if mdd < 0 else np.nan
    yearly = equity.resample("YE").last().pct_change().dropna()
    return {
        "Kapitał końcowy": f"{equity.iloc[-1]:,.0f}",
        "Całkowity zwrot": f"{total:.1%}",
        "CAGR": f"{cagr:.2%}",
        "Zmienność roczna": f"{vol:.2%}",
        "Sharpe": f"{sharpe:.2f}",
        "Sortino": f"{sortino:.2f}",
        "Max drawdown": f"{mdd:.2%}",
        "Szczyt -> dołek MDD": f"{peak.date()} -> {trough.date()}",
        "Odrobienie MDD": str(rec.date()) if rec is not None else "nie odrobiono",
        "Calmar": f"{calmar:.2f}",
        "Najlepszy rok": f"{yearly.max():.1%} ({yearly.idxmax().year})" if len(yearly) else "n/a",
        "Najgorszy rok": f"{yearly.min():.1%} ({yearly.idxmin().year})" if len(yearly) else "n/a",
        "Lata dodatnie": f"{(yearly > 0).mean():.0%}" if len(yearly) else "n/a",
    }


def trade_stats(trades: pd.DataFrame, df: pd.DataFrame) -> dict:
    if trades.empty:
        return {"Liczba transakcji": 0}
    closed = trades[~trades["open"]]
    r = closed["return_pct"]
    wins, losses = r[r > 0], r[r <= 0]
    gross_pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.nan

    # Najdłuższe serie
    def longest_streak(mask: pd.Series) -> int:
        best = cur = 0
        for v in mask:
            cur = cur + 1 if v else 0
            best = max(best, cur)
        return best

    exposure = (df["position"].shift(1, fill_value=0) > 0).mean()
    return {
        "Liczba transakcji (zamkniętych)": len(closed),
        "Otwarta pozycja na koniec": "TAK" if trades["open"].any() else "NIE",
        "Winrate": f"{len(wins) / len(r):.1%}" if len(r) else "n/a",
        "Zyskowne / stratne": f"{len(wins)} / {len(losses)}",
        "Średni zwrot na transakcję": f"{r.mean():.2f}%",
        "Mediana zwrotu": f"{r.median():.2f}%",
        "Średni zysk (wygrane)": f"{wins.mean():.2f}%" if len(wins) else "n/a",
        "Średnia strata (przegrane)": f"{losses.mean():.2f}%" if len(losses) else "n/a",
        "Payoff ratio (śr. zysk / śr. strata)": f"{wins.mean() / abs(losses.mean()):.2f}"
        if len(wins) and len(losses) and losses.mean() != 0
        else "n/a",
        "Profit factor": f"{gross_pf:.2f}",
        "Expectancy": f"{(len(wins) / len(r)) * wins.mean() + (len(losses) / len(r)) * losses.mean():.2f}%"
        if len(wins) and len(losses)
        else "n/a",
        "Najlepsza transakcja": f"{r.max():.2f}% ({closed.loc[r.idxmax(), 'entry_date'].date()})" if len(r) else "n/a",
        "Najgorsza transakcja": f"{r.min():.2f}% ({closed.loc[r.idxmin(), 'entry_date'].date()})" if len(r) else "n/a",
        "Max seria zysków": longest_streak(r > 0),
        "Max seria strat": longest_streak(r <= 0),
        "Śr. czas trwania (sesje)": f"{closed['sessions'].mean():.0f}",
        "Śr. czas wygranej (sesje)": f"{closed.loc[r > 0, 'sessions'].mean():.0f}",
        "Śr. czas przegranej (sesje)": f"{closed.loc[r <= 0, 'sessions'].mean():.0f}",
        "Ekspozycja na rynek": f"{exposure:.1%}",
        "Transakcji na rok": f"{len(trades) / ((df.index[-1] - df.index[0]).days / 365.25):.2f}",
        "Przecięć MA w trakcie blokady": int((df["blocked"] & (df["cross_up"] | df["cross_down"])).sum()),
        "Transakcji opóźnionych przez blokadę": int((df["blocked"].shift(1, fill_value=False)
                                                     & ~df["blocked"]
                                                     & (df["signal_pos"].diff() != 0)).sum()),
        "Liczba zleceń (kupno + sprzedaż)": int((df["cost"] > 0).sum()),
        "Koszty transakcyjne łącznie": f"{df['cost'].sum():,.2f} ({df['cost'].sum() / df['equity'].iloc[-1]:.2%} kapitału końc.)",
        "Śr. koszt na zlecenie": f"{df.loc[df['cost'] > 0, 'cost_frac'].mean():.3%}" if (df["cost"] > 0).any() else "n/a",
        "Śr. koszt na transakcję (round-trip)": f"{(closed['gross_return_pct'] - r).mean():.2f} pp",
    }


def print_table(title: str, data: dict) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    width = max(len(k) for k in data)
    for k, v in data.items():
        print(f"{k:<{width}} : {v}")


def print_comparison(strat: dict, bh: dict, name: str = "") -> None:
    print(f"\n{'=' * 70}\n{name + ': ' if name else ''}STRATEGIA MAC vs BUY & HOLD\n{'=' * 70}")
    width = max(len(k) for k in strat)
    print(f"{'':<{width}}   {'Strategia':>24}   {'Buy & Hold':>24}")
    for k in strat:
        print(f"{k:<{width}}   {strat[k]:>24}   {bh[k]:>24}")


# --------------------------------------------------------------------------- #
# Wykresy
# --------------------------------------------------------------------------- #
def make_charts(df: pd.DataFrame, trades: pd.DataFrame, cfg: Config) -> None:
    out = cfg.out_dir
    plt.style.use("seaborn-v0_8-whitegrid")
    closed = trades[~trades["open"]]

    # 1. Cena + średnie + sygnały
    fig, ax = plt.subplots(figsize=(16, 8))
    ax.plot(df.index, df["Close"], lw=0.8, color="black", label=cfg.name)
    ax.plot(df.index, df["MA_fast"], lw=1.0, color="tab:blue", label=f"MA{cfg.fast}")
    ax.plot(df.index, df["MA_slow"], lw=1.2, color="orange", label=f"MA{cfg.slow}")
    ax.fill_between(df.index, df["Close"].min() * 0.9, df["Close"].max() * 1.1,
                    where=df["position"] > 0, color="green", alpha=0.07, label="W pozycji")
    ax.scatter(trades["entry_date"], trades["entry_price"], marker="^", color="green", s=40, zorder=5, label="Kupno")
    ax.scatter(closed["exit_date"], closed["exit_price"], marker="v", color="red", s=40, zorder=5, label="Sprzedaż")
    ax.set_yscale("log")
    ax.set_ylim(df["Close"].min() * 0.9, df["Close"].max() * 1.1)
    ax.set_title(f"{cfg.name} – przecięcia MA{cfg.fast} / MA{cfg.slow}, sygnały transakcyjne (skala log)")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out / "01_price_sma_signals.png", dpi=130)
    plt.close(fig)

    # 2. Krzywa kapitału + drawdown
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10), sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
    ax1.plot(df.index, df["equity"], color="tab:blue", lw=1.3, label=f"Strategia MA{cfg.fast}/MA{cfg.slow}")
    ax1.plot(df.index, df["bh_equity"], color="tab:gray", lw=1.0, label="Buy & Hold")
    ax1.set_yscale("log")
    ax1.set_title(f"{cfg.name} – krzywa kapitału (start {cfg.initial_capital:,.0f}, skala log)")
    ax1.legend(loc="upper left")
    dd_s = df["equity"] / df["equity"].cummax() - 1
    dd_b = df["bh_equity"] / df["bh_equity"].cummax() - 1
    ax2.fill_between(df.index, dd_b * 100, 0, color="tab:gray", alpha=0.4, label="Buy & Hold")
    ax2.fill_between(df.index, dd_s * 100, 0, color="tab:blue", alpha=0.6, label="Strategia")
    ax2.set_ylabel("Drawdown [%]")
    ax2.set_title("Obsunięcie kapitału")
    ax2.legend(loc="lower left")
    fig.tight_layout()
    fig.savefig(out / "02_equity_drawdown.png", dpi=130)
    plt.close(fig)

    # 3. Zwroty poszczególnych transakcji w czasie
    fig, ax = plt.subplots(figsize=(16, 6))
    colors = np.where(trades["return_pct"] > 0, "green", "red")
    ax.bar(trades["entry_date"], trades["return_pct"], width=60, color=colors)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Zwrot z każdej transakcji (data wejścia)")
    ax.set_ylabel("%")
    ax.xaxis.set_major_locator(mdates.YearLocator(5))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.tight_layout()
    fig.savefig(out / "03_trade_returns_timeline.png", dpi=130)
    plt.close(fig)

    # 4. Rozkład zwrotów z transakcji + czas trwania vs zwrot
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))
    r = closed["return_pct"]
    bins = np.linspace(r.min(), r.max(), 40) if len(r) > 1 else 10
    ax1.hist(r[r <= 0], bins=bins, color="red", alpha=0.7, label="Stratne")
    ax1.hist(r[r > 0], bins=bins, color="green", alpha=0.7, label="Zyskowne")
    ax1.axvline(r.mean(), color="black", ls="--", label=f"Średnia {r.mean():.2f}%")
    ax1.set_title("Rozkład zwrotów z transakcji")
    ax1.set_xlabel("%")
    ax1.legend()
    ax2.scatter(closed["sessions"], r, c=np.where(r > 0, "green", "red"), alpha=0.7)
    ax2.axhline(0, color="black", lw=0.8)
    if (closed["sessions"] > 0).any():
        ax2.set_xscale("log")
    ax2.set_title("Czas trwania transakcji vs zwrot")
    ax2.set_xlabel("Liczba sesji (log)" if ax2.get_xscale() == "log" else "Liczba sesji")
    if closed.empty:
        ax2.text(0.5, 0.5, "Brak zamkniętych transakcji", ha="center", transform=ax2.transAxes)
    ax2.set_ylabel("%")
    fig.tight_layout()
    fig.savefig(out / "04_trade_distribution.png", dpi=130)
    plt.close(fig)

    # 5. Roczne stopy zwrotu
    ys = df["equity"].resample("YE").last()
    yb = df["bh_equity"].resample("YE").last()
    ys = ys.pct_change().fillna(ys.iloc[0] / cfg.initial_capital - 1) * 100
    yb = yb.pct_change().fillna(yb.iloc[0] / cfg.initial_capital - 1) * 100
    years = ys.index.year
    x = np.arange(len(years))
    fig, ax = plt.subplots(figsize=(18, 6))
    ax.bar(x - 0.2, ys.values, 0.4, label="Strategia", color="tab:blue")
    ax.bar(x + 0.2, yb.values, 0.4, label="Buy & Hold", color="tab:gray")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(years, rotation=90)
    ax.set_title("Roczne stopy zwrotu")
    ax.set_ylabel("%")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "05_annual_returns.png", dpi=130)
    plt.close(fig)

    # 6. Kroczący 3-letni CAGR
    win = TRADING_DAYS * 3
    roll_s = ((df["equity"] / df["equity"].shift(win)) ** (1 / 3) - 1) * 100
    roll_b = ((df["bh_equity"] / df["bh_equity"].shift(win)) ** (1 / 3) - 1) * 100
    fig, ax = plt.subplots(figsize=(16, 6))
    ax.plot(roll_s, label="Strategia", color="tab:blue")
    ax.plot(roll_b, label="Buy & Hold", color="tab:gray")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Kroczący 3-letni CAGR")
    ax.set_ylabel("%")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "06_rolling_3y_cagr.png", dpi=130)
    plt.close(fig)

    print(f"\nWykresy zapisane w: {out.resolve()}")


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> tuple[Config, int | None]:
    p = argparse.ArgumentParser(description="Backtest strategii MAC (MA50 / MA200) wyłącznie na sWIG80")
    p.add_argument("--years", type=int, default=Config.years)
    p.add_argument("--fast", type=int, default=Config.fast, help="okres krótkiej średniej")
    p.add_argument("--slow", type=int, default=Config.slow, help="okres długiej średniej")
    p.add_argument("--commission", type=float, default=Config.commission,
                   help="prowizja na stronę transakcji, np. 0.0029 (domyślnie profil indeksu)")
    p.add_argument("--min-commission", type=float, default=Config.min_commission,
                   help="minimalna prowizja w walucie indeksu (domyślnie profil indeksu)")
    p.add_argument("--spread", type=float, default=Config.spread,
                   help="pełny spread bid/ask, np. 0.004; każda strona płaci połowę (domyślnie profil indeksu)")
    p.add_argument("--cash-rate", type=float, default=Config.cash_rate, help="roczne oprocentowanie gotówki")
    p.add_argument("--delay", type=int, default=Config.delay, help="opóźnienie wykonania w sesjach")
    p.add_argument("--cooldown", type=int, default=Config.cooldown, help="blokada transakcji po każdej transakcji [sesje]")
    p.add_argument("--capital", type=float, default=Config.initial_capital)
    p.add_argument("--out", type=Path, default=Path(__file__).parent / "output",
                   help="katalog bazowy wyników; raporty trafią do podkatalogu sWIG80")
    p.add_argument("--optimize", type=int, nargs="?", const=60, default=None, metavar="MAX",
                   help="przeszukaj cooldown 0..MAX (domyślnie 60) i użyj wartości maksymalizującej CAGR")
    a = p.parse_args(argv)
    for key in ("years", "fast", "slow"):
        if getattr(a, key) <= 0:
            p.error(f"--{key} musi być dodatnie")
    if a.fast >= a.slow:
        p.error("--fast musi być mniejsze niż --slow")
    for key in ("delay", "cooldown", "optimize"):
        value = getattr(a, key)
        if value is not None and value < 0:
            p.error(f"--{key} nie może być ujemne")
    for key in ("commission", "min_commission", "spread", "cash_rate", "capital"):
        if not math.isfinite(getattr(a, key)):
            p.error(f"--{key.replace('_', '-')} musi być liczbą skończoną")
    if not 0 <= a.commission < 1 or not 0 <= a.spread < 2 or a.commission + a.spread / 2 >= 1:
        p.error("--commission i --spread muszą dawać koszt procentowy w zakresie [0, 100%)")
    if a.capital <= 0 or not 0 <= a.min_commission < a.capital:
        p.error("--capital musi być dodatnie, a --min-commission w zakresie [0, kapitał)")
    if a.min_commission / a.capital + a.spread / 2 >= 1:
        p.error("minimalny koszt zlecenia musi być mniejszy niż kapitał")
    if a.cash_rate <= -1:
        p.error("--cash-rate musi być większe niż -1")
    cfg = Config(
        years=a.years, fast=a.fast, slow=a.slow, cash_rate=a.cash_rate,
        commission=a.commission, min_commission=a.min_commission, spread=a.spread,
        delay=a.delay, cooldown=a.cooldown, initial_capital=a.capital,
        out_dir=a.out / "sWIG80",
    )
    return cfg, a.optimize


def cagr_of(equity: pd.Series, initial: float) -> float:
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    return (equity.iloc[-1] / initial) ** (1 / years) - 1


def optimize_cooldown(df: pd.DataFrame, cfg: Config, max_cd: int) -> int:
    rows = []
    for cd in range(max_cd + 1):
        res, trades = run_backtest(df, replace(cfg, cooldown=cd))
        rows.append({
            "cooldown": cd,
            "CAGR": cagr_of(res["equity"], cfg.initial_capital),
            "MaxDD": (res["equity"] / res["equity"].cummax() - 1).min(),
            "trades": len(trades),
        })
    opt = pd.DataFrame(rows).set_index("cooldown")
    best = int(opt["CAGR"].idxmax())
    opt.to_csv(cfg.out_dir / "cooldown_optimization.csv")

    print(f"\n{'=' * 70}\n{cfg.name}: OPTYMALIZACJA COOLDOWN (0-{max_cd} sesji) - TOP 10 wg CAGR\n{'=' * 70}")
    top = opt.sort_values("CAGR", ascending=False).head(10)
    print(top.assign(CAGR=top["CAGR"].map("{:.2%}".format), MaxDD=top["MaxDD"].map("{:.1%}".format)).to_string())
    print(f"\nNajlepszy cooldown: {best} sesji (CAGR {opt.at[best, 'CAGR']:.2%})")

    fig, ax1 = plt.subplots(figsize=(14, 6))
    ax1.plot(opt.index, opt["CAGR"] * 100, marker="o", ms=3, color="tab:blue", label="CAGR")
    ax1.axvline(best, color="green", ls="--", label=f"Optimum = {best}")
    ax1.set_xlabel("Cooldown [sesje]")
    ax1.set_ylabel("CAGR [%]", color="tab:blue")
    ax2 = ax1.twinx()
    ax2.plot(opt.index, opt["MaxDD"] * 100, color="tab:red", alpha=0.6, label="Max DD")
    ax2.set_ylabel("Max drawdown [%]", color="tab:red")
    ax2.grid(False)
    ax1.set_title(f"{cfg.name} – wpływ długości blokady (cooldown) na CAGR i max drawdown")
    h1, l1 = ax1.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax1.legend(h1 + h2, l1 + l2, loc="lower left")
    fig.tight_layout()
    fig.savefig(cfg.out_dir / "07_cooldown_optimization.png", dpi=130)
    plt.close(fig)
    return best


def run_instrument(cfg: Config, optimize: int | None) -> dict:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)

    df = download_data(cfg)
    if optimize is not None:
        cfg = replace(cfg, cooldown=optimize_cooldown(df, cfg, optimize))
    print(f"\n{cfg.name} – cooldown: {cfg.cooldown} sesji | koszty na zlecenie: prowizja {cfg.commission:.2%} "
          f"(min {cfg.min_commission:g}), spread {cfg.spread:.2%} (płacona połowa)")
    df, trades = run_backtest(df, cfg)

    strat = performance_stats(df["strat_ret"], df["equity"], cfg)
    bh = performance_stats(df["bh_ret"], df["bh_equity"], cfg)
    tstats = trade_stats(trades, df)
    print_comparison(strat, bh, cfg.name)
    print_table(f"{cfg.name}: STATYSTYKI TRANSAKCJI", tstats)

    pd.set_option("display.width", 200)
    print(f"\n{cfg.name} – ostatnie 10 transakcji:")
    num_cols = trades.select_dtypes("number").columns
    print(trades.tail(10).round({c: 2 for c in num_cols}).to_string(index=False))

    trades.round({c: 4 for c in num_cols}).to_csv(cfg.out_dir / "trades.csv", index=False)
    df.round(6).to_csv(cfg.out_dir / "daily_backtest.csv")
    make_charts(df, trades, cfg)
    return {"cfg": cfg, "df": df, "strat": strat, "bh": bh, "trades": tstats,
            "bh_cost": cfg.initial_capital * transaction_cost_frac(cfg.initial_capital, cfg)}


SUMMARY_TRADE_KEYS = [
    "Liczba transakcji (zamkniętych)", "Winrate", "Średni zwrot na transakcję", "Profit factor",
    "Ekspozycja na rynek", "Koszty transakcyjne łącznie",
]


def summarize(result: dict, out_dir: Path) -> None:
    start, end = result["df"].index[0].date(), result["df"].index[-1].date()
    period = {"Okres": f"{start} - {end}", "Cooldown [sesje]": str(result["cfg"].cooldown)}
    bh_cost = {k: "" for k in SUMMARY_TRADE_KEYS}
    bh_cost["Koszty transakcyjne łącznie"] = f"{result['bh_cost']:,.2f}"
    table = pd.DataFrame({
        "sWIG80 MAC": {
            **period, **result["strat"],
            **{k: str(result["trades"].get(k, "")) for k in SUMMARY_TRADE_KEYS},
        },
        "sWIG80 B&H": {**period, **result["bh"], **bh_cost},
    })
    table.to_csv(out_dir / "summary.csv", encoding="utf-8-sig")


def main() -> None:
    cfg, optimize = parse_args()
    result = run_instrument(cfg, optimize)
    summarize(result, cfg.out_dir)


if __name__ == "__main__":
    main()

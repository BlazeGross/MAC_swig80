# MAC: analiza sWIG80

Skrypt wsadowy analizuje **wylacznie sWIG80**, korzystajac z historii zamkniec
GPW Benchmark (ISIN `PL9999999979`). Strategia MAC domyslnie uzywa MA50/MA200
i jest porownywana z Buy & Hold tego samego indeksu. Nie jest to aplikacja WWW
ani system skladania zlecen.

## Uruchomienie lokalne

Wspierane srodowisko wdrozeniowe: **Python 3.13**, Linux, CPU amd64 lub arm64.
Testy CI sa wykonywane na Linuxie; lokalnie mozna pracowac na Windowsie.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe swig80_backtest.py
.\.venv\Scripts\python.exe swig80_backtest.py --optimize 60
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Na Linuxie bez Dockera:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python swig80_backtest.py --out ./output
```

Pelna lista opcji: `python swig80_backtest.py --help`.
`--out` wskazuje katalog **bazowy**; skrypt dodaje do niego `sWIG80`.
Domyslnie jest to katalog `output` obok skryptu.
Nie ma juz opcji wyboru indeksu ani poprzedniego punktu wejscia.

## Wyniki i zalozenia

W `output/sWIG80/` powstaja:

- `daily_backtest.csv`: ceny, srednie, pozycje, koszty i krzywe kapitalu;
- `trades.csv`: transakcje, takze ewentualna otwarta pozycja;
- `summary.csv`: statystyki MAC oraz Buy & Hold dla sWIG80;
- szesc wykresow PNG; przy `--optimize MAX` dodatkowo
  `cooldown_optimization.csv` i siodmy wykres.

Koszty domyslne: prowizja 0.29%, minimum 5 PLN, spread 0.40%
(polowa na strone transakcji), kapital 10 000. Mozna je nadpisac opcjami CLI.
Pierwsze wejscie nastepuje dopiero po golden cross; poza rynkiem portfel
pozostaje w gotowce. Cooldown i opoznienie zachowuja dotychczasowa semantyke.
Daty zakresu pobierania sa liczone w `Europe/Warsaw`, niezaleznie od strefy serwera.

To symulacja na **indeksie cenowym**, nie notowania ETF ani indeks dochodowy.
Nie uwzglednia podatkow, dywidend, tracking error i historycznej dostepnosci ETF.
Realizacja na zamknieciu sesji sygnalowej jest zalozeniem modelowym;
`--delay 1` pozwala modelowac wykonanie na kolejnej sesji.
Optymalizacja cooldown na tej samej probie nie jest walidacja out-of-sample.
Najlepszy okres kalendarzowy w statystykach wymaga co najmniej dwoch
rocznych obserwacji koncowych; w krotszych probach pojawia sie `n/a`.

GPW musi byc dostepna przez HTTPS. Bledy sieci i nieprawidlowe dane przerywaja
proces z niezerowym kodem; nie ma zastepczego zrodla danych ani ukrytego cache.
Nie uruchamiaj dwoch analiz do tego samego katalogu jednoczesnie. Ponowne
uruchomienie nadpisuje raporty; dla archiwum stosuj osobne katalogi `--out`.
Zachowuj parametry polecenia i identyfikator obrazu wraz z raportami.

## Linux: wdrozenie Docker na zadanie

Wymagany Docker Engine. Obraz dziala bez GUI, jako uzytkownik `10001:10001`;
nie wymaga portow, bazy danych, sekretow aplikacyjnych ani dostepu SSH z CI.
W kontenerze zapisywalne musza byc tylko katalog wynikow i `/tmp`.

Budowa i test punktu wejscia:

```bash
docker build --pull -t swig80-backtest:local .
docker run --rm swig80-backtest:local --help
```

Przygotuj katalog na serwerze (przykladowa sciezka, uprawnienia tylko dla
uzytkownika kontenera):

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /srv/swig80/output
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,src=/srv/swig80/output,dst=/app/output \
  swig80-backtest:local
```

Dodatkowe argumenty zastepuja `CMD`, nie `ENTRYPOINT`:

```bash
docker run --rm --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --cap-drop ALL --security-opt no-new-privileges \
  --mount type=bind,src=/srv/swig80/output,dst=/app/output \
  swig80-backtest:local --out /app/output --years 22 --optimize 60
```

Domyslnie brak harmonogramu. Kontener konczy prace po zapisaniu raportow;
kod wyjscia `0` oznacza sukces. Logi sa wysylane na stdout/stderr.
Zadbaj o retencje i backup katalogu wynikow oraz dostep do Internetu
(`gpwbenchmark.pl`, DNS i HTTPS). Nie montuj calego projektu ani gniazda Dockera
do kontenera analitycznego.

## GitLab: pierwsze przeniesienie

Utworz pusty, najlepiej prywatny projekt GitLab i wlacz Container Registry.
Ten katalog nie byl repozytorium Git; inicjalizacje i publikacje wykonaj
swiadomie, po przegladzie plikow:

```bash
git init -b main
git add .
git status --short
git diff --cached
git commit -m "Prepare sWIG80 analysis for Linux and GitLab"
git remote add origin git@gitlab.example.com:group/swig80-backtest.git
git push -u origin main
```

Zastap adres swoim repozytorium. `.gitignore` wyklucza wyniki, srodowiska
wirtualne, ustawienia edytora i pliki `.env`. `.dockerignore` dopuszcza do
kontekstu obrazu tylko skrypt i blokade zaleznosci. Nie dodawaj tokenow,
kluczy SSH ani lokalnych danych do repozytorium.

### Pipeline

1. `test`: instalacja z blokady, kontrola zaleznosci i testy offline na Linuxie.
   Testy obejmuja CLI, pobieranie z mockiem HTTP, koszty, sygnaly, cooldown,
   opoznienie, optymalizacje i komplet raportow, takze bez zamknietych transakcji.
2. `build-image`: po testach, tylko na domyslnej galezi i tagach, buduje obraz,
   sprawdza `--help` i publikuje do Registry z tagiem pelnego SHA commita.
3. `analyze`: reczny, nieblokujacy job na tych samych referencjach. Uruchamia
   zbudowany obraz z dostepem do GPW. Raporty sa artefaktami przez 30 dni.
   Status bledu joba pozostaje widoczny, choc nie blokuje pipeline.

**Wymagania runnera:** Linux Docker executor. Budowa wykorzystuje Docker-in-Docker
z TLS; wymaga zaufanego runnera z `privileged = true` oraz wspoldzielonym
katalogiem certyfikatow `/certs/client` (np. `volumes = ["/certs/client", "/cache"]`
w sekcji `[runners.docker]` konfiguracji administratora).
Ogranicz runner do zaufanego projektu; nie uzywaj uprzywilejowanej budowy
do niezaufanych pipeline z forkow. Zmienne `CI_REGISTRY_*` dostarcza GitLab;
nie wpisuj hasel w YAML. Registry i runner musza byc skonfigurowane przed
pierwszym pipeline.

Na serwerze pobierz obraz po SHA, nie po ruchomym tagu `latest`:

```bash
docker login registry.gitlab.example.com
docker pull registry.gitlab.example.com/group/swig80-backtest:COMMIT_SHA
```

Do pobierania prywatnych obrazow uzyj Deploy Token z samym `read_registry`
i `docker login --password-stdin` w automatyzacji. Uruchamiaj jak w przykladach
wyzej, zastepujac nazwe obrazu. Wersja poprzedniego SHA umozliwia rollback.
Nie skonfigurowano automatycznego wdrazania na serwer ani publikacji repozytorium.

## Zaleznosci i aktualizacje

`requirements.in` opisuje bezposrednie zaleznosci. `requirements.txt` jest
blokada wszystkich zaleznosci z hashami, wygenerowana przez pip-tools 7.6.2
na Pythonie 3.13. Jawne `tzdata` zapewnia baze stref czasowych w slim Linux.
Yahoo Finance nie jest zaleznoscia projektu.

Po swiadomej zmianie wersji w `requirements.in`, w osobnym srodowisku narzedziowym:

```bash
python -m pip install pip-tools==7.6.2
python -m piptools compile --generate-hashes --strip-extras --no-emit-index-url --no-emit-trusted-host --output-file requirements.txt requirements.in
```

Zacommituj oba pliki, uruchom testy i zbuduj obraz na docelowej architekturze.
Tag obrazu bazowego Pythona jest celowo aktualizowalny dla poprawek systemowych;
identyfikator obrazu wynikowego w Registry pozwala wdrazac konkretny build.
Nie deklarujemy bitowej powtarzalnosci obrazu bazowego bez przypiecia jego digestu.

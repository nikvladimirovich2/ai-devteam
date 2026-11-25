[English](DEPLOY.md) · [Русский](DEPLOY.ru.md) · **Srpski** · [中文](DEPLOY.zh.md)

# Vodič za deployment

Instalacija AI DevTeam-a na čistom hostu, korak po korak. Traje ~15 minuta.

**Brzi način** je instaler jednom komandom iz [README](README.md#quick-start) fajla: on preuzima
gotovu sliku (`ghcr.io/nikvladimirovich2/ai-devteam`), pokreće tim i otvara browser, gde
čarobnjak za podešavanje (`⚙ Setup`) traži ključ za model i GitHub/GitLab token. Ovaj vodič je namenjen serverima,
zatvorenim mrežama i svemu što se umesto toga može podesiti u `.env`.

**Windows?** Pokrenite ga preko WSL container CLI-ja (`wslc`), bez Docker Desktop-a: pogledajte
[11. Windows: WSL kontejneri (wslc)](#11-windows-wsl-kontejneri-wslc).

## 1. Zahtevi

| | Minimum | Preporučeno |
|---|---|---|
| OS | Linux x86_64 (Debian 12/13, Ubuntu 22.04+, Astra, RHEL 9) | — |
| CPU / RAM | 4 vCPU / 8 GB | 8 vCPU / 16 GB (agenti pokreću pip/npm/pytest) |
| Disk | 20 GB | 50 GB (slika ~1.7 GB + projekti + pip/npm keševi) |
| Softver | Docker Engine 24+, Docker Compose v2, git | — |
| Mreža | HTTPS ka LLM API-ju i ka GitLab/GitHub-u; pypi.org / registry.npmjs.org za agente | — |

Sam LLM **nije** deo ovog steka — agenti pozivaju postojeći OpenAI-kompatibilan endpoint
(vLLM, Ollama, LiteLLM proxy, GigaChat, …). Na ovom hostu nije potreban GPU.

## 2. Instalacija Docker-a

Debian / Ubuntu (paketi iz distribucije):

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose docker-buildx git curl jq
sudo systemctl enable --now docker
docker version && docker compose version
```

(Ili Docker CE sa download.docker.com — radi bilo koji Docker 24+ sa `compose` pluginom.)
Korisnik koji nije root: `sudo usermod -aG docker $USER` i ponovo se prijavite.

## 3. Preuzimanje koda

```bash
sudo mkdir -p /opt/devteam && sudo chown $USER: /opt/devteam
git clone https://github.com/<you>/ai-devteam.git /opt/devteam
cd /opt/devteam
```

## 4. Podešavanje `.env`

```bash
cp .env.example .env
chmod 600 .env
echo "INTERNAL_SECRET=$(openssl rand -hex 32)" >> .env
$EDITOR .env
```

Obavezan je samo `INTERNAL_SECRET`. Model, ključ i Git mogu se podesiti **ili** ovde **ili** u
browseru (`⚙ Setup` → *Check and save*). Vrednosti iz `.env` imaju prednost; čarobnjak ih prikazuje kao «set in .env».
Podešavanja iz čarobnjaka čuvaju se u volumenima: model i ključ u `leaddata` (lead), Git token u
`devopsdata` (samo devops, režim 0600, shell korisnik agenata ne može da ga čita).

Podešavanje u `.env`:

| Promenljiva | Primer | Napomene |
|---|---|---|
| `LLM_MODEL` | `hosted_vllm/Qwen/Qwen3-32B-AWQ` | LiteLLM format `<provider>/<model id from /v1/models>` |
| `LLM_BASE_URL` | `https://llm.corp.local/v1` | za vLLM/OpenAI-kompatibilne API-je — uključujući `/v1` |
| `LLM_API_KEY` | | jedan ključ za ceo tim… |
| `LEAD_API_KEY` … `DEVOPS_API_KEY` | | …ili 6 ličnih ključeva, po jedan za svaku ulogu (imaju prednost nad `LLM_API_KEY`) |
| `GIT_URL` | `https://gitlab.corp.local` ili `https://github.com` | provajder se određuje iz URL-a |
| `GIT_TOKEN` | | pogledajte «Git tokeni» ispod |
| `GIT_NAMESPACE` | `ai-team` | GitLab grupa / GitHub organizacija (prazno = lični nalog vlasnika tokena) |
| `INTERNAL_SECRET` | `openssl rand -hex 32` | ≥ 32 karaktera; potpisuje zahteve između kontejnera (pogledajte ispod) |

Preporučeno:

| Promenljiva | Zašto |
|---|---|
| `UI_USER`, `UI_PASSWORD` | Basic Auth za web UI |
| `ARCHITECT_MODEL`, `BACKEND_MODEL`, … | drugačiji model za svaku ulogu — pogledajte [MODELS.md](MODELS.md) |

Ograničenja jednog pokretanja zadatka (opciono; isto u `⚙ Setup` → *Limits*, vrednosti iz `.env` imaju prednost):

| Promenljiva | Podrazumevano | Značenje |
|---|---|---|
| `TASK_BUDGET_USD` | prazno = bez ograničenja | najveća cena jednog pokretanja (novi zadatak ili dorada), u američkim dolarima. Cene iz LiteLLM tabele; lokalni i nepoznati modeli se računaju kao $0 |
| `TASK_BUDGET_TOKENS` | prazno = bez ograničenja | najviše tokena jednog pokretanja (broje se za svaki model, i za lokalne) |
| `TASK_TIME_LIMIT_MIN` | 180 | ukupno vreme jednog pokretanja, u minutima; zatim se tim zaustavlja (status «time limit»). `0` = bez ograničenja |
| `SHELL_TIMEOUT` | 300 | jedna shell komanda agenta, u sekundama; zatim se ubija zajedno sa svim podprocesima, a agent dobija jasnu TIMEOUT grešku (samo `.env`) |
| `LLM_TIMEOUT` | 600 | jedan zahtev ka modelu, u sekundama (samo `.env`) |

Kada se ograničenje potroši, vođa tima zaustavlja tim pre sledećeg koraka (status «budget used up») i piše završnu
poruku sa potrošenim iznosom i linkom ka repozitorijumu. Provera se radi između koraka agenata, pa poslednji korak može
malo da pređe ograničenje. Cena svakog koraka, ukupan iznos u izveštaju i «💸» u zaglavlju zadatka vide se i bez ograničenja.

**Stop** prekida korak koji je u toku: shell komanda agenta se ubija zajedno sa podprocesima, svaki sledeći poziv alata
se odbija, a sledeći poziv modela se prekida, pa se zadatak završava kao «cancelled» za nekoliko sekundi. Zahtev ka
modelu koji je već poslat se ne prekida — prvo se završi (ili dostigne `LLM_TIMEOUT`). Vremensko ograničenje zaustavlja
tim na isti način.

> **`$` u vrednostima.** Compose razvija `$VAR` unutar `.env`. Ključ koji sadrži `$` mora biti u jednostrukim navodnicima:
> `QA_API_KEY='$abc#def'`, inače se deo ključa tiho odbacuje.

### Izolacija agenata

Agenti izvršavaju shell komande koje piše model, a prompt injection (README nekog paketa, web
stranica, tekst zadatka) može naterati model da pokrene bilo šta. Dva sloja drže ključeve i Git token dalje
od tih komandi:

- **Poseban shell korisnik.** Servis radi kao `agent` (uid 1000) i drži API ključ uloge (a,
  u `devops`, i `GIT_TOKEN`). Svaka komanda iz `run_shell` izvršava se kao `runner` (uid 1001) sa čistim
  okruženjem, pa `/proc/<pid>/environ` i memorija agenta nisu čitljivi. Oba korisnika
  dele grupu, tako da oba mogu da menjaju fajlove projekta. Git token se prosleđuje `git push`-u kroz
  okruženje, nikad u komandnoj liniji (`/proc/<pid>/cmdline` može da čita svaki korisnik).
- **Potpisani interni zahtevi.** Lead i workeri potpisuju svaki interni zahtev HMAC tokenom
  specifičnim za ulogu, izvedenim iz `INTERNAL_SECRET`. Bez njega `POST devops:8000/publish` i
  `POST lead:8000/api/internal/events` vraćaju `403`, pa komanda ne može da objavljuje pod DevOps
  tokenom niti da pošalje lažni `✅ pushed` događaj. Uloga događaja uzima se iz tokena, a ne iz
  tela zahteva. Worker drži samo sopstveni token; sama tajna ostaje u lead-u.

`scripts/check.sh` proverava oba sloja (`shell user cannot read the agent environment`, `internal
endpoints reject unsigned calls`). Ipak: dajte tokenu samo prava koja su mu potrebna, po mogućstvu za
posebnu organizaciju / nalog.

### Git tokeni

**GitLab** — Personal ili Group Access Token, scope-ovi `api` + `write_repository`
(korisnik mora imati dozvolu da kreira projekte u `GIT_NAMESPACE`).

**GitHub** — fine-grained PAT: Settings → Developer settings → Fine-grained tokens:
- Resource owner = organizacija iz `GIT_NAMESPACE` (ili vi sami), **Repository access: All repositories**
  (agenti kreiraju nove repozitorijume — «Only select» ih ne obuhvata);
- Repository permissions: **Administration RW, Contents RW, Workflows RW, Actions R** (Metadata R je automatski).
- U organizaciji: fine-grained tokeni moraju biti dozvoljeni (i odobreni ako je odobravanje uključeno), a SAML SSO autorizovan.

Alternativa sa klasičnim PAT-om: scope-ovi `repo` + `workflow`.

### Self-signed / korporativni sertifikati

Ako se LLM ili GitLab služe preko HTTPS-a sa privatnim CA:

```bash
cp /path/to/ca-or-server.crt agent/certs/       # PEM, -----BEGIN CERTIFICATE-----
docker compose restart
```

Folder (`CERTS_DIR`, podrazumevano `./agent/certs`; instaleri koriste `./certs`) montira se na `/certs` u svakom
kontejneru; pri pokretanju se sertifikati dodaju u bundle pouzdanih sertifikata za Python (certifi,
`REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`), git (`GIT_SSL_CAINFO`) i node (`NODE_EXTRA_CA_CERTS`). Rebuild nije potreban.
Kada sami gradite sliku, sertifikati iz `agent/certs/` se takođe ugrađuju u nju.
SAN sertifikata mora sadržati host iz `LLM_BASE_URL` (za IP — `IP Address:`).
`agent/certs/*.crt` je u git-ignore: kopirajte ga na svaki host.

## 5. Zahtevi za LLM server

Agenti rade preko **tool calls** (`write_file`, `run_shell`, `publish_repository`).
Endpoint modela mora vraćati nativne `tool_calls`:

- **vLLM**: pokrenite sa `--enable-auto-tool-choice --tool-call-parser <parser>`
  (`hermes` za Qwen2.5/Qwen3, `llama3_json` za Llama 3.x, `mistral` za Mistral, …).
- **Ollama**: koristite model sa `tools` mogućnošću.
- Kontekst: preporučuje se ≥ 32k tokena (`--max-model-len 32768`); veličina prozora se automatski čita iz
  vLLM `/v1/models` (`max_model_len`) ili se podešava preko `LLM_CONTEXT_WINDOW`.

Brza ručna provera:

```bash
curl -s --cacert agent/certs/llm.crt "$LLM_BASE_URL/models" -H "Authorization: Bearer <key>" | jq '.data[].id'
```

### Server modela na istom računaru (Ollama, LM Studio, vLLM)

Unutar kontejnera `127.0.0.1` / `localhost` je **sam kontejner**, a ne vaš računar, pa
`http://127.0.0.1:11434` ne stiže do Ollama koja radi pored tima. U `⚙ Setup` (ili u `LLM_BASE_URL`) ipak
možete da upišete `localhost`: agenti ga sami zamenjuju adresom računara — `host.docker.internal` u Docker-u,
IP adaptera `vEthernet (WSL)` u wslc na Windows-u (`wslc-up.ps1` ga pronalazi i prosleđuje kao `HOST_GATEWAY`).
Na serveru modela su potrebne dve stvari:

1. **Slušati na svim interfejsima**, a ne samo na `127.0.0.1` (podrazumevano za Ollama):

   | Sistem | Ollama |
   |---|---|
   | Windows | `setx OLLAMA_HOST 0.0.0.0`, zatim zatvorite Ollama u traci i pokrenite je ponovo |
   | Linux (systemd) | `sudo systemctl edit ollama` → `[Service]` `Environment="OLLAMA_HOST=0.0.0.0"`, zatim `sudo systemctl restart ollama` |
   | macOS | `launchctl setenv OLLAMA_HOST 0.0.0.0`, zatim ponovo pokrenite aplikaciju Ollama |

   LM Studio: *Developer → Server settings → Serve on local network*. vLLM: `--host 0.0.0.0`.
2. **Dozvoliti u zaštitnom zidu, samo za kontejnere.** Windows (PowerShell kao administrator; ime adaptera
   prikazuje `Get-NetAdapter`):

   ```powershell
   New-NetFirewallRule -DisplayName "Ollama for AI DevTeam" -Direction Inbound -Protocol TCP -LocalPort 11434 -InterfaceAlias "vEthernet (WSL)" -Action Allow
   ```

   Linux sa ufw: `sudo ufw allow in on docker0 to any port 11434 proto tcp` (i za most compose mreže,
   `ip link | grep br-`).

Slušanje na `0.0.0.0` čini Ollama dostupnom i iz lokalne mreže, a Ollama nema lozinku: ograničite pravilo
zaštitnog zida na adapter kontejnera, kao gore. Ako provera u `⚙ Setup` kaže da server modela na ovom
računaru ne odgovara kontejnerima, nije urađen jedan od ova dva koraka.

## 6. Pokretanje

```bash
docker compose up -d         # pulls the prebuilt image
docker compose ps            # 6 containers, all (healthy) after ~30 s
./scripts/check.sh           # end-to-end pre-flight check
```

Nemate pristup ghcr.io (zatvorena mreža)? Umesto toga izgradite sliku na hostu:
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`,
ili je učitajte iz fajla: `docker save ghcr.io/nikvladimirovich2/ai-devteam:latest | gzip > devteam.tgz` na mašini
sa pristupom internetu, zatim `docker load < devteam.tgz`.

`scripts/check.sh` proverava: da je `.env` popunjen i `$`-navodnike, sertifikate, zdravlje kontejnera,
**da svaka uloga dopire do LLM-a sopstvenim ključem**, da nativni tool calling radi, Git token/namespace,
da je Git token prisutan samo u `devops`, zdravlje UI-ja.

## 7. Otvaranje UI-ja

UI sluša samo na `127.0.0.1:${UI_PORT:-8080}`. Sa vaše radne stanice:

```bash
ssh -L 8080:localhost:8080 user@<host>                      # direct
ssh -J jump-user@jump-host:port -L 8080:localhost:8080 user@<host>   # via a jump host
# → http://localhost:8080
```

Da biste ga izložili na mreži: `UI_BIND=0.0.0.0` **i** `UI_USER`/`UI_PASSWORD`, po mogućstvu iza
reverse proxy-ja sa TLS-om (nginx/traefik).

## 8. Prvi zadatak

Prvo pošaljite nešto malo (pogledajte «Example task» u README-u). U feed-u bi trebalo da vidite:
`🔎 architect: +N files` → `🔎 backend/frontend: …` → QA presuda → `✅ pushed …` → `⏳ Waiting for CI…` →
`✅ CI passed` → završni izveštaj u običnom tekstu sa tokenima po ulozi.

## 9. Operacije

```bash
docker compose logs -f lead devops         # logs
docker compose restart                     # after editing .env (or: up -d to recreate)
docker compose pull && docker compose up -d   # update (source builds: git pull + the build file)
docker compose down                        # stop, data kept
docker compose down -v                     # stop and DELETE task history and workspaces
```

Backup — dva volumena:

```bash
docker run --rm -v devteam_leaddata:/d -v $PWD:/b alpine tar czf /b/leaddata.tgz -C /d .
docker run --rm -v devteam_workspace:/d -v $PWD:/b alpine tar czf /b/workspace.tgz -C /d .
```

## 10. Rešavanje problema

| Simptom | Uzrok / rešenje |
|---|---|
| `The "X" variable is not set` pri `docker compose` | vrednost u `.env` sadrži `$` — stavite je u jednostruke navodnike |
| `CERTIFICATE_VERIFY_FAILED` | stavite CA/serverski sertifikat u folder sa sertifikatima (`CERTS_DIR`), `docker compose restart` |
| Čarobnjak kaže «The key was rejected» / «does not call tools» | poruka objašnjava šta treba uraditi; tehnički detalji su ispod nje |
| `401` od LLM-a | pogrešan `<ROLE>_API_KEY`; `./scripts/check.sh` pokazuje koja uloga |
| Agent je «prijavio» posao, ali `🔎 … did not create files` | model ne vraća tool calls → vLLM flagovi iznad, `LLM_FUNCTION_CALLING=auto` |
| `Action 'write_file' don't exist` / petlje | slab model za tool calling za ovu ulogu → koristite jači (MODELS.md) |
| `GitLab namespace … not found` | pogrešan `GIT_NAMESPACE` ili korisnik tokena nema pristup njemu |
| GitHub `403` pri kreiranju repozitorijuma | fine-grained token nije odobren u organizaciji / nema Administration RW / SSO nije autorizovan |
| `⚠ CI: no_pipeline` | CI konfiguracija nije push-ovana, CI je isključen u projektu ili nema runnera |
| CI crven posle `MAX_CI_ROUNDS` | otvorite zadatak → **🔧 CI** da pokrenete još rundi ispravki, ili ispravite ručno |
| Lead kontejner `unhealthy`, UI zamrznut | `docker compose logs lead`; `docker compose restart lead` (zadatak u toku postaje `interrupted`) |
| `INTERNAL_SECRET is missing or shorter than 32 characters` | dodajte `INTERNAL_SECRET=$(openssl rand -hex 32)` u `.env`, `docker compose up -d` |
| `403 internal endpoint` u logovima | kontejneri su pokrenuti sa različitim `INTERNAL_SECRET` vrednostima (stari kontejneri): ponovo kreirajte sve sa `docker compose up -d --force-recreate` |

## 11. Windows: WSL kontejneri (wslc)

`wslc.exe` je container CLI ugrađen u WSL. Pokreće istu sliku u laganoj Linux virtuelnoj mašini,
bez Docker Desktop-a. Testirano na Windows 10 22H2 (build 19044) sa WSL 3.0.3.

### 11.1 Zahtevi

| | Minimum | Napomene |
|---|---|---|
| Windows | Windows 10 2004+ (build 19041) ili Windows 11, x64 | |
| WSL | **3.0.1 ili noviji** (`wsl --version`) | `wslc` dolazi uz WSL, ništa drugo ne treba instalirati |
| Virtualizacija | VT-x/AMD-V u BIOS-u + funkcija «Virtual Machine Platform» | unutar VM-a: ugnježdena virtualizacija |
| RAM | 8 GB na hostu | izmereno: ~2 GB za 6 neaktivnih agenata. wslc VM podrazumevano dobija 4 GB (`wslc settings`); kada pip/npm/pytest rade paralelno, povećajte na 6 GB |
| Disk | 10 GB slobodno na disku sa `%LOCALAPPDATA%\wslc` | slika 1.66 GB, disk VM-a `storage.vhdx` naraste do ~3.3 GB posle build-a, plus projekti i keševi |
| Softver | PowerShell 5.1+ (ugrađen), git | `docker` / `docker compose` **nisu** potrebni |
| Mreža | LLM endpoint, GitLab/GitHub, pypi.org, registry.npmjs.org | VM koristi Windows mrežu, uključujući VPN |

LLM nije deo steka: laptop bez GPU-a sasvim dobro radi sve dok `LLM_BASE_URL` pokazuje na server sa modelom.

### 11.2 Instalacija / ažuriranje WSL-a

U PowerShell-u **kao administrator**, samo ako WSL nikada nije bio instaliran:

```powershell
wsl --install --no-distribution     # enables «Virtual Machine Platform»; restart Windows afterwards
```

Zatim (kao običan korisnik):

```powershell
wsl --update
wsl --version                       # WSL version: 3.0.1 or newer
wslc version
wslc run --rm hello-world           # smoke test of the container runtime
```

### 11.3 Instalacija (jedna komanda)

U PowerShell-u (administratorska prava nisu potrebna kada je WSL već instaliran):

```powershell
irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 | iex
```

Instaler:
- pokreće `wsl --update` ako `wslc` nedostaje (i kaže šta treba uraditi ako WSL uopšte nije instaliran);
- stavlja skripte za pokretanje/zaustavljanje u `%USERPROFILE%\ai-devteam`, bira slobodan port (8080, 8081, …) za `.env`;
- preuzima gotovu sliku (~1.7 GB) i pokreće šest kontejnera;
- otvara browser na čarobnjaku za podešavanje i pravi prečicu **AI DevTeam** na desktopu (za pokretanje posle restarta).

Sertifikati kompanije: `%USERPROFILE%\ai-devteam\certs\*.crt`, zatim ponovo pokrenite preko prečice.

### 11.4 Iz izvornog koda (za developere)

```powershell
git clone https://github.com/nikvladimirovich2/ai-devteam.git
cd ai-devteam
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build   # builds the image here (~8 min)
```

Specifičnosti `.env` na Windows-u (opciono, pogledajte [4. Podešavanje `.env`](#4-podešavanje-env)):

- `INTERNAL_SECRET` može ostati prazan: skripta za pokretanje ga generiše i upisuje u `.env`.
- Skripta čita vrednosti doslovno, pa `$` u ključevima ne zahteva navodnike (navodnici su i dalje prihvaćeni).
- Port 8080 je često zauzet na developerskoj mašini: podesite npr. `UI_PORT=8099`.
  `netstat -ano | findstr :8099` pokazuje da li je port slobodan.
- Korporativni sertifikati idu u `agent\certs\*.crt` (ili `CERTS_DIR`).
- Ne commit-ujte `.env`. On je u `.gitignore`. Da biste ga ograničili na svog korisnika: `icacls .env /inheritance:r /grant:r "$($env:USERNAME):F"`.
- Git sa `core.autocrlf=true` (podrazumevano u Git for Windows) checkout-uje fajlove sa CRLF.
  Ovo je testirano: slika se gradi i radi. Skripta sama upisuje env fajlove po ulogama sa LF.

wslc još nema `compose`, pa `scripts\wslc-up.ps1` radi ono što radi `docker-compose.yml`: jednu mrežu
`devteam`, volumene `devteam_workspace`, `devteam_leaddata` i `devteam_devopsdata`, šest kontejnera. Svaki
kontejner dobija **samo sopstveni** ključ; Git token dobija samo `devteam-devops`. Privremeni env fajlovi
se brišu odmah posle svakog pokretanja. Skripta odbija `UI_BIND` različit od `127.0.0.1` bez `UI_USER`/`UI_PASSWORD`.

```powershell
wslc list                                                              # 6 containers, (healthy) after ~30 s
```

Smoke test bez modela (koristi `tests/mock_llm.py`; objavljivanje ne uspeva bez Git hosta, što je očekivano):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\wslc-up.ps1 -Build -Mock
```

Pošaljite mali zadatak u UI-ju: feed prikazuje architect → backend/frontend → QA (pytest) → DevOps,
a zadatak se završava kao `done_no_push`.

Bezbednosna provera (ista kao u `check.sh`):

```powershell
wslc exec -u runner devteam-devops sh -c "cat /proc/1/environ > /dev/null && echo LEAK || echo ok"   # ok
wslc exec -u runner devteam-backend curl -s -o /dev/null -w "%{http_code}" -X POST http://devops:8000/publish -d "{}"   # 403
```

Sam `scripts/check.sh` poziva `docker` i ne radi sa wslc.

### 11.5 Operacije

```powershell
wslc logs -f devteam-lead                                   # logs (any devteam-<role>)
wslc stats                                                  # CPU / memory per container
.\scripts\wslc-up.ps1                                       # after editing .env: recreates the containers
.\scripts\wslc-up.ps1                                       # update: downloads the latest image (source: git pull; -Build)
.\scripts\wslc-down.ps1                                     # stop, task history and projects kept
.\scripts\wslc-down.ps1 -Volumes                            # stop and DELETE history and projects
wslc settings                                               # VM CPU / memory / storage location
```

Razlike u odnosu na Docker Compose:

- **Nema restart policy.** Posle restarta Windows-a ponovo pokrenite `wslc-up.ps1` (to radi prečica na desktopu koju pravi instaler). Volumeni ostaju sačuvani.
  Za automatsko pokretanje: Task Scheduler → «At log on» → `powershell -ExecutionPolicy Bypass -File <path>\scripts\wslc-up.ps1`.
- Skripta ne pokreće test GitLab iz `tests/docker-compose.test.yml` (zahteva ~4 GB RAM-a).
- Backup: `wslc run --rm -v devteam_leaddata:/d -v ${PWD}:/b alpine tar czf /b/leaddata.tgz -C /d .`
  (isto i za `devteam_workspace`).

### 11.6 Oslobađanje prostora na disku

```powershell
.\scripts\wslc-down.ps1 -Volumes
wslc rmi devteam-agent:latest
wslc image prune -a -f
```

Brisanje slika ne smanjuje disk VM-a `%LOCALAPPDATA%\wslc\sessions\<session>\storage.vhdx`
(~3 GB posle build-a). Da biste povratili prostor: `wslc system session terminate`, zatim obrišite taj
`storage.vhdx`. wslc pravi novi prazan disk pri sledećem pokretanju. **Sve slike i volumeni te
sesije se gube.**

### 11.7 Rešavanje problema (Windows)

| Simptom | Uzrok / rešenje |
|---|---|
| `wslc` nije pronađen | WSL stariji od 3.0.1: `wsl --update` |
| `running scripts is disabled on this system` | pokrenite sa `powershell -ExecutionPolicy Bypass -File ...` |
| `bind: address already in use` / UI nije dostupan | port zauzima druga aplikacija: promenite `UI_PORT` |
| Kontejner se gasi odmah posle pokretanja | `wslc logs devteam-<role>`; obično prazan `LLM_MODEL` ili pogrešan ključ |
| Build pada na `pip install` / `apt-get` sa TLS greškama | korporativni proxy/CA: stavite CA u `agent\certs\`, ponovo izgradite sa `-Rebuild` |
| Agenti su spori ili bivaju ubijeni tokom testova | memorija VM-a: `wslc settings` → povećajte memoriju, zatim `wslc system session terminate` |
| Izmene u `.env` se ne primenjuju | ponovo pokrenite `wslc-up.ps1`: on ponovo kreira kontejnere |

## 12. Zaustavljanje, pokretanje, ažuriranje, uklanjanje

Zaustavljanje ništa ne briše: istorija zadataka, folderi projekata, ključevi i tokeni iz `⚙ Setup` ostaju.
Briše ih samo **uklanjanje sa podacima**. Repozitorijumi već objavljeni na GitHub/GitLab se nikada ne diraju.

### Windows (instalirano preko `install.ps1`)

| Radnja | Kako |
|---|---|
| Zaustavi | dupli klik na prečicu **AI DevTeam - stop** na radnoj površini, ili `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\wslc-down.ps1"` |
| Pokreni (i posle restarta Windows-a) | dupli klik na prečicu **AI DevTeam** na radnoj površini |
| Ažuriraj na najnoviju verziju | ponovo pokreni instaler: `irm https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.ps1 \| iex` (podaci i podešavanja ostaju) |
| Stanje, logovi | `wslc list` · `wslc logs -f devteam-lead` |
| Ukloni, zadrži podatke | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1" -KeepData` |
| Ukloni potpuno | `powershell -ExecutionPolicy Bypass -File "$HOME\ai-devteam\scripts\uninstall.ps1"` (pita pre brisanja podataka; `-Yes` = bez pitanja) |

`uninstall.ps1` uklanja kontejnere, mrežu, image (~1,7 GB), obe prečice sa radne površine i folder
`ai-devteam`, a bez `-KeepData` i volumene `devteam_workspace`, `devteam_leaddata`, `devteam_devopsdata`. Ako
nije ostao nijedan drugi WSL kontejner ni image, briše i disk virtuelne mašine WSL kontejnera
(`%LOCALAPPDATA%\wslc\sessions\…\storage.vhdx`), čime se vraća preostali prostor na disku. Sam WSL ostaje instaliran.

> **Običan PowerShell i PowerShell administratora.** WSL čuva kontejnere običnog korisnika i administratora
> odvojeno (dve «sesije» sa sopstvenim podacima). Instalirajte i pokrećite AI DevTeam u **običnom** PowerShell-u —
> tako rade i prečice na radnoj površini; kao administrator instaler se ne pokreće. `wslc-down.ps1` i
> `uninstall.ps1` čiste sve sesije kojima mogu da pristupe; pokrenuti kao administrator, vide i običnu.

### Linux / macOS (instalirano preko `install.sh`)

```bash
cd ~/ai-devteam
docker compose stop          # stop (data kept)
docker compose start         # start again
docker compose ps            # status
docker compose logs -f lead  # logs
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash   # update
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash -s -- --uninstall              # remove everything
curl -fsSL https://raw.githubusercontent.com/nikvladimirovich2/ai-devteam/main/install.sh | bash -s -- --uninstall --keep-data  # keep the data volumes
```

Posle restarta kontejneri se sami pokreću (`restart: unless-stopped`), osim ako ste ih zaustavili.

### Instalirano iz izvornog koda

```bash
docker compose down                          # stop and remove the containers, data kept
docker compose down -v                       # … and delete the data volumes
docker compose down -v --rmi all             # … and the image
docker image prune -f                        # leftovers of local builds
```

Izvorni kod na Windows-u: `.\scripts\wslc-down.ps1` (zaustavi), `.\scripts\wslc-down.ps1 -Volumes` (i podaci),
`.\scripts\uninstall.ps1` (sve osim samog foldera).

# Deployment pe Northflank

## Pași pentru deployment

### 1. Creează cont pe Northflank
- Mergi la https://app.northflank.com/
- Creează cont gratuit
- Conectează repository-ul GitHub

### 2. Creează un nou service
- Click pe "New Service"
- Selectează "From Git Repository"
- Alege repository-ul `contrarian-pullback-bot`
- Selectează branch-ul `main`

### 3. Configurează build
- **Build Type**: Dockerfile
- **Dockerfile Path**: `Dockerfile`
- **Region**: `eu-central-1` (Frankfurt, Germany) pentru IP european

### 4. Configurează environment variables (MANUAL - OBLIGATORIU)

**IMPORTANT:** Northflank nu importă automat variabilele din `northflank.yaml`. Trebuie să le adaugi manual în UI.

**Pași pentru a adăuga variabile:**

1. În service-ul tău, mergi la secțiunea **"Environment"** (din sidebar-ul stâng)
2. Click pe **"+ Add variable"** sau butonul similar
3. Adaugă fiecare variabilă una câte una:

**Obligatorii (trebuie create ca SECRETS):**
- `BYBIT_API_KEY` - Cheia ta Bybit API
  - Click pe "Create as secret" sau marchează ca "Secret"
  - Valoare: cheia ta Bybit API
- `BYBIT_API_SECRET` - Secretul tău Bybit API
  - Click pe "Create as secret" sau marchează ca "Secret"
  - Valoare: secretul tău Bybit API

**Configurare (variabile normale):**
- `BYBIT_TESTNET` = `false`
- `SYMBOLS` = `BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT`
- `POSITION_SIZE_USDT` = `100`
- `LEVERAGE` = `20`
- `EMA_PERIOD_4H` = `200`
- `ST_PERIOD_4H` = `10`
- `ST_MULTIPLIER_4H` = `3.0`
- `ST_PERIOD_1H` = `10`
- `ST_MULTIPLIER_1H` = `3.0`
- `PORT` = `10000`
- `CHECK_INTERVAL_SECONDS` = `300`
- `UPDATE_4H_HOURS` = `4`

**Notă:** După ce adaugi toate variabilele, click pe **"Save"** pentru a le aplica.

### 5. Configurează port și health check
- **Port**: `10000`
- **Health Check Path**: `/api/status`
- **Health Check Interval**: `30` secunde

### 6. Configurează auto-deploy
- **Auto-deploy**: Enabled
- **Branch**: `main`
- La fiecare push pe `main`, botul se va redeploya automat

### 7. Deploy
- Click pe "Deploy"
- Așteaptă build-ul și deploy-ul
- Verifică logs pentru erori

## Verificare după deployment

1. **Verifică status**: Mergi la dashboard și verifică că service-ul rulează
2. **Verifică logs**: Click pe "Logs" pentru a vedea output-ul botului
3. **Testează health check**: Accesează `https://your-service.northflank.app/api/status`
4. **Verifică dashboard**: Accesează `https://your-service.northflank.app/` pentru dashboard

## Notițe importante

- **IP European**: Service-ul va rula în Frankfurt (eu-central-1) pentru IP european
- **Keep-alive**: Health check-ul va menține service-ul activ
- **Auto-deploy**: Fiecare push pe `main` va declanșa un nou deploy
- **Secrets**: `BYBIT_API_KEY` și `BYBIT_API_SECRET` trebuie marcate ca "Secret" în Northflank

## Troubleshooting

### Botul nu pornește
- Verifică logs pentru erori
- Verifică că toate environment variables sunt setate corect
- Verifică că `BYBIT_API_KEY` și `BYBIT_API_SECRET` sunt valide

### Health check eșuează
- Verifică că portul `10000` este expus corect
- Verifică că endpoint-ul `/api/status` răspunde

### Deploy eșuează
- Verifică că Dockerfile este corect
- Verifică că toate dependențele sunt în `requirements.txt`
- Verifică logs de build pentru erori


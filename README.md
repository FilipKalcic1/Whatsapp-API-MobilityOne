
## Brzi Start 

### 1. Konfiguracija  
Kopiraj primjer konfiguracije i popuni potrebne podatke:

```bash
cp .env.example .env
````

U `.env` obavezno postavi:

* `OPENAI_API_KEY`
* Infobip podatke: `INFOBIP_API_KEY`, `INFOBIP_BASE_URL`, `INFOBIP_SECRET`
* MobilityOne podatke: `MOBILITYONE_CLIENT_ID`, `MOBILITYONE_SECRET`

Bez ovoga sustav se neće pravilno pokrenuti.


### 2. Pokretanje Sustava

Podiži cijeli stack (API, Worker, Redis, Baza, Monitoring):

```bash
docker-compose up --build -d
```

API će nakon pokretanja biti dostupan na:
**[http://localhost:8000](http://localhost:8000)**

---

### 3.  Povezivanje s WhatsAppom (Ngrok)

Budući da API radi lokalno, moraš ga izložiti internetu kako bi Infobip mogao isporučivati poruke.

Pokreni ngrok tunel:

```bash
ngrok http 8000
```

Kopiraj generirani **HTTPS URL** — npr.:
`https://a1b2-c3d4.ngrok-free.app`

Zatim u Infobip dashboardu:

1. Idi na **Channels & Numbers → WhatsApp**
2. Odaberi svog WhatsApp pošiljatelja
3. Pod **Incoming messages (Webhook)** dodaj:

   ```
   https://TVOJ-NGROK-URL.ngrok-free.app/webhook/whatsapp
   ```

Time je WhatsApp integracija spremna za rad.

---

## Monitoring i Status

* **API Healthcheck:** [http://localhost:8000/health](http://localhost:8000/health)
* **Grafana Dashboard:** [http://localhost:3000](http://localhost:3000)

  * Username: `admin`
  * Lozinka: definirana u `.env`
* **Prometheus metrike:** [http://localhost:8001](http://localhost:8001)

---

## Gašenje Sustava

```bash
docker-compose down
```

---

Ako trebaš verziju s dijagramima ili dodatnim primjerima konfiguracije, samo javi!

```
```

# YellowGrid – integrare comunitară, versiunea 1.19.3b9 (beta publică)

## Configurare

- Număr de telefon: `07xxxxxxxx` (sunt acceptate și `7xxxxxxxx`, `+407xxxxxxxx`, `00407xxxxxxxx`).
- Parola / codul de conectare folosit în aplicația YellowGrid.
- Interval de actualizare: 5–60 de minute.

Integrarea trimite `countryCode = "004"` și `mobileNumber = "07xxxxxxxx"`.
Nu solicită introducerea unei chei API. Integrarea preia automat configurația
clientului din serviciul `https://utilitati-yellowgrid-config.marius-onitiu.workers.dev/v1/yellowgrid/config`.
Valoarea nu este inclusă în sursele publice și este ținută temporar numai în
memoria sesiunii Home Assistant (cache de până la 6 ore). Numărul de telefon,
parola și tokenurile nu sunt transmise acestui serviciu; autentificarea
utilizatorului și citirile rămân directe către API-ul YellowGrid.

Endpointul Cloudflare este public și transmite cheia clientului către
instalările Home Assistant: cheia poate fi recuperată din trafic de oricine
cunoaște adresa. Această abordare permite actualizarea centralizată, dar **nu**
asigură confidențialitatea cheii și nu constituie autorizare de utilizare din
partea YellowGrid. Dacă serviciul de configurare nu funcționează, conexiunile
noi pot eșua; o cheie deja obținută poate fi folosită temporar din memoria
sesiunii. În cazul unei respingeri 401/403 la login, integrarea verifică dacă
există o cheie actualizată și reîncearcă doar dacă valoarea s-a schimbat.


## Funcționalități

- Descoperirea automată a instalațiilor și citirea telemetriei, inclusiv SOC.
- Producție, consum, import/export și încărcare/descărcare baterie.
- Fluxuri energetice pentru ziua calendaristică din România, în kWh.
- Soldul portofelului, disponibilul pentru retragere și suma neachitată, în RON.
- Filă YellowGrid afișată numai când furnizorul este configurat.
- Diagramă animată cu sensul fluxurilor și grafice circulare zilnice.
- Pentru un dispozitiv offline, puterile instantanee apar indisponibile,
  în timp ce datele energetice istorice pot rămâne disponibile.

## Siguranță și diagnostic

Se efectuează doar citiri de date și operațiuni de autentificare/reînnoire a
sesiunii, fără comenzi către invertor. Tokenurile sunt păstrate în memoria
sesiunii. În timpul configurării, fișierul de diagnostic local
`/config/utilitati_romania_yellowgrid_diagnostic.json` poate înregistra etapele
și codurile HTTP, dar nu parola, telefonul, tokenurile, cheia API sau
răspunsurile brute.

## Stadiul testării

Autentificarea fără introducerea manuală a cheii, crearea dispozitivelor,
portofelul, datele energetice și dashboardul au fost verificate într-o
instalare Home Assistant reală. A fost verificat și comportamentul la
indisponibilitatea YellowBox. Testarea live s-a făcut pe un singur cont.
Rămân necesare verificări cu instalații multiple, reautentificare după perioade
îndelungate și eventuale schimbări ale API-ului.

Integrare comunitară neoficială. Aceasta nu este o integrare oferită sau
garantată de YellowGrid. Efectele unei eventuale modificări a API-ului nu
pot fi excluse.

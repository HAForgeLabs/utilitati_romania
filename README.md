# Utilități România

Integrare unificată pentru Home Assistant care centralizează utilitățile din România într-o singură experiență coerentă.

Permite gestionarea facturilor, notificărilor și transmiterii indexului direct din Home Assistant, indiferent de furnizor.

---

## Funcționalități

- Facturi centralizate pentru mai mulți furnizori
- Suport multi-locație și multi-contract
- Card Lovelace dedicat
- Notificări automate pentru:
  - facturi noi
  - deschiderea perioadei de transmitere index
- Transmitere index direct din dashboard
- Detectare automată a perioadelor de citire
- Deschidere rapidă a aplicațiilor furnizorilor
- Grupare facturi pe locații
- Actualizare manuală a datelor
- Marcare manuală a facturilor ca plătite
- Sistem integrat de licențiere
- Integrare nativă Home Assistant

---

## Interfață

### Card facturi utilități

![Card facturi](./docs/card.png)

---

### Integrarea în Home Assistant

![Integrare](./docs/integrare.png)

---

### Device & administrare

![Administrare](./docs/integrare2.png)

---

## Furnizori suportați

- E.ON
- Hidroelectrica
- myElectrica
- Digi
- Nova
- Apă Canal Sibiu
- e-bloc.ro
- Orange Romania

Lista furnizorilor este în continuă extindere.

---

## Instalare

### Instalare prin HACS

1. Deschide HACS
2. Accesează `Integrations`
3. Adaugă repository-ul custom:

   `https://github.com/HAForgeLabs/utilitati_romania`

4. Instalează integrarea
5. Restart Home Assistant

---

## Configurare

1. Accesează `Settings → Devices & Services`
2. Selectează `Add Integration`
3. Caută `Utilități România`

---

## Card Lovelace

```yaml
type: custom:utilitati-romania-facturi-card
```

---

## Funcții importante

### Transmitere index

Integrarea detectează automat perioadele active de transmitere și permite trimiterea indexului direct din card.

---

### Notificări automate

Sunt generate notificări pentru:

- emiterea unor facturi noi
- deschiderea perioadelor de transmitere index

---

### Grupare facturi

Mai multe contracte sau locații pot fi grupate manual astfel încât facturile aferente aceleiași locații să fie afișate împreună.

Configurarea se face din device-ul `Grupare facturi`.

---

### Deschidere aplicații furnizori

Integrarea poate deschide direct aplicațiile mobile ale furnizorilor compatibili din Home Assistant.

---

## Licențiere

Integrarea include:

- trial gratuit 90 zile
- licență lifetime

După expirarea perioadei trial, integrarea continuă să funcționeze cu funcționalități limitate până la activarea unei licențe.

---

## Susține proiectul

Dacă integrarea îți este utilă și dorești să susții dezvoltarea și mentenanța proiectului:

`https://buymeacoffee.com/haforgelabs`

---

## Atribuire și componente derivate

Această integrare reprezintă un proiect unificat și refactorizat pentru Home Assistant, dezvoltat pentru a integra mai mulți furnizori de utilități din România într-o singură integrare coerentă.

Anumite componente, în special logica de comunicare API pentru unii furnizori, sunt derivate sau inspirate din proiecte open-source publicate de Cristian Necrea sub licență MIT:

- https://github.com/cnecrea/eonromania
- https://github.com/cnecrea/hidroelectrica
- https://github.com/cnecrea/myelectrica

Componentele derivate din aceste proiecte sunt utilizate cu respectarea licenței MIT, inclusiv prin păstrarea notificărilor de copyright în fișierele relevante.

Restul integrării, inclusiv:

- arhitectura unificată
- agregarea furnizorilor
- cardul Lovelace
- logica de notificări
- administrarea integrării
- gruparea facturilor
- sistemul de licențiere
- integrarea e-bloc.ro
- interfața și funcționalitățile specifice proiectului

reprezintă contribuții proprii ale proiectului Utilități România.

---

## Disclaimer

Integrare neoficială pentru Home Assistant.

Nu este afiliată, susținută sau aprobată oficial de furnizorii integrați.

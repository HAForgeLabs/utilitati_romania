# YellowGrid - versiune beta v1.19.3b6

Versiune de test, construita pe baza Utilitati Romania v1.19.2. Nu este destinata publicarii.

## Configurare in Home Assistant

La adaugarea YellowGrid, introdu numarul de telefon in format 07xxxxxxxx
(10 cifre, cu zero-ul national) si parola / codul de conectare din aplicatia mobila.
Pentru comoditate, se accepta si 7xxxxxxxx, +407xxxxxxxx sau 00407xxxxxxxx:
integrarea normalizeaza automat la 07xxxxxxxx pentru campul `mobileNumber`.

`countryCode` este transmis automat cu valoarea `004`, conform formatului
comunicat pentru contul testat. Nu exista camp pentru prefixul tarii in UI.

Limitare beta: cheia `x-apikey` trebuie inca introdusa separat. Este utilizata
exclusiv pentru testarea privata; nu se publica si nu se include in cod sursa.
Pentru lansarea publica este necesara o metoda sustenabila/autorizata de
obtinere sau aprovizionare a acestei chei, fara interventia utilizatorului.

## Separarea autentificarii de licentiere

Numarul de telefon YellowGrid nu inlocuieste identitatea globala de licenta
Utilitati Romania. Daca exista o licenta globala salvata, integrarea continua
sa o valideze cu identitatea deja asociata acesteia.

## Date si comportament

- Descoperire automata a instalatiilor
- Puteri instantanee, baterie si fluxuri zilnice in kWh
- Sold portofel, disponibil pentru retragere si suma neachitata in RON
- Actualizare configurabila de la 5 la 60 de minute
- Numai GET pentru date; POST folosit exclusiv pentru login/refresh
- Tokenuri pastrate in memorie, fara comenzi catre invertor

Datele energetice zilnice se pot revizui pe server; verificarea statisticilor
Home Assistant este necesara inainte de folosirea lor in Energy Dashboard.

## Testare

Copiaza intai versiunea HA existenta si pastreaza arhiva stabila.
Dupa instalare este necesar un singur restart Home Assistant pentru incarcarea
codului nou. Poti incerca autentificarea de mai multe ori fara alte restarturi.

La fiecare incercare de conectare, rezultatul se salveaza in:
`/config/utilitati_romania_yellowgrid_diagnostic.json`

Raportul include operatia, etapa, codul HTTP, durata si categoria erorii.
Nu include telefonul, parola, cheia `x-apikey`, identificatori de instalatie,
tokenuri sau corpuri de raspuns. Fisierul este suprascris la incercarea urmatoare.
Jurnalul HA contine acelasi ID de diagnostic. In caz de eroare trimite doar
acest JSON, fara capturi cu formularul si fara credentiale.

Autentificarea corecta trimite `countryCode="004"` si
`mobileNumber="07xxxxxxxx"`. Include zero-ul national.

## Panou vizual v1.19.3b6

- Flux energetic original, creat din elemente HTML/CSS, nu din resursele aplicației mobile.
- Grafice circulare pentru distribuția producției și a consumului, generate din senzorii energetici existenți.
- Toate fluxurile detaliate rămân disponibile prin extinderea secțiunii aferente.
- Contoarele generale și celelalte file rămân neschimbate.
- Valorile de 0% raportate de API sunt afișate ca atare, fără corecții artificiale.
- Nu se modifică autentificarea, colectarea datelor sau dispozitivele.

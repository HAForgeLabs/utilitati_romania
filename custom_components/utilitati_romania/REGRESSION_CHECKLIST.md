# Verificari de regresie

Acest fisier se actualizeaza pentru orice modificare care poate influenta mai multi furnizori sau module comune.

## Reguli obligatorii

- Modificarile din `coordonator.py`, `facturi_agregate.py`, `sensor.py`, `notificari.py` si frontend trebuie analizate pentru toti furnizorii care folosesc acel flux.
- O regula specifica unui furnizor trebuie izolata prin cheia furnizorului si nu introdusa ca fallback generic.
- Totalurile se calculeaza doar din campuri de rest de plata confirmate; istoricul nu devine automat neachitat.
- Statusul unei facturi se stabileste prioritar din indicatorul explicit al portalului.
- Pentru fiecare beta se noteaza scenariul testat si rezultatul.

## v1.10.13b8 - refresh E.ON conform HAR si jurnal local extins

### Fisiere modificate

- `furnizori/eon_api.py`
- `coordonator.py`
- fisierele de versiune frontend si manifest

### Domeniu

- Furnizor afectat direct: E.ON Romania.
- Modul comun afectat: coordonatorul, numai in taskul dedicat E.ON.
- Alti furnizori afectati: niciunul in mod intentionat.

### Dovezi din HAR

- `POST /users/v1/userauth/refresh-token` foloseste payload-ul `{"token":"<accessToken fara prefix>"}`.
- Preflight-ul pentru refresh declara explicit headerul `authorization`.
- Codul JavaScript E.ON seteaza `withAuthBearer: true` pentru refresh.
- Headerul real este `Authorization: Bearer <accessToken>`.
- Raspunsul de refresh returneaza un nou `accessToken`, folosit la urmatorul refresh.
- Tokenul este rotativ si trebuie persistat dupa fiecare raspuns reusit.

### Modificare

- Refresh-ul trimite acelasi access token in doua forme:
  - fara prefix in payload;
  - cu prefixul `Bearer` in headerul `Authorization`.
- Tokenul nou este aplicat si persistat dupa fiecare refresh.
- Jurnalul local extins include requesturile si raspunsurile complete pentru login, MFA, injectare, export, refresh si persistenta.

### Riscuri

- Jurnalul contine parola, codul MFA si tokenuri complete.
- Versiunea nu trebuie distribuita si nu trebuie folosita dupa finalizarea investigatiei.
- Logurile trebuie sterse dupa test.

### Verificari obligatorii

- o reautentificare completa cu 2FA;
- primul refresh la aproximativ 18-28 minute;
- minimum trei refresh-uri consecutive cu HTTP 200;
- confirmarea ca tokenul din raspuns devine tokenul urmatoarei cereri;
- restart Home Assistant si continuarea refresh-ului;
- rulare minimum 24 de ore fara reautentificare.

### Rezultat

- Test nereusit: primul refresh a primit `6047 Invalid refresh token`.
- Jurnalul a aratat `Authorization: bearer <accessToken>`.
- Frontendul E.ON normalizeaza intotdeauna schema la `Bearer`, chiar daca API-ul raspunde cu `tokenType: bearer`.

## v1.10.13b9 - schema Authorization E.ON identica frontendului

### Fisiere modificate

- `furnizori/eon_api.py`
- fisierele de versiune frontend si manifest
- `REGRESSION_CHECKLIST.md`

### Domeniu

- Furnizor afectat direct: E.ON Romania.
- Module comune afectate: niciunul.
- Alti furnizori afectati: niciunul.

### Dovezi

- Raspunsul MFA local contine `tokenType: bearer`.
- In b8, aceasta valoare a fost folosita direct si s-a trimis `Authorization: bearer <accessToken>`.
- Codul frontend E.ON transforma orice `tokenType` egal cu `bearer`, fara diferenta de litere, in valoarea canonica `Bearer <accessToken>`.
- Preflight-ul HAR confirma ca requestul de refresh contine headerul `Authorization`.
- Payload-ul ramane `{"token":"<accessToken fara prefix>"}`.

### Modificare

- Schema de autentificare E.ON este normalizata la `Bearer`.
- Refresh-ul trimite exact `Authorization: Bearer <accessToken>`.
- Toate requesturile autentificate E.ON folosesc acelasi helper, pentru a evita diferente intre endpointuri.
- Logul local extins ramane activ pentru verificarea requestului si a raspunsului.

### Riscuri

- Modificarea este izolata in clientul E.ON.
- Jurnalul contine date sensibile si versiunea nu trebuie distribuita.

### Verificari obligatorii

- reautentificare completa cu 2FA;
- confirmarea in log a headerului `Authorization: Bearer ...`;
- primul refresh cu HTTP 200;
- minimum trei refresh-uri consecutive cu token rotit;
- restart Home Assistant si continuarea sesiunii;
- rulare minimum 24 de ore fara reautentificare.

### Rezultat

- In asteptarea testului local.

## v1.10.13b7 - persistenta sesiune E.ON

### Fisiere modificate

- `furnizori/eon_api.py`
- `furnizori/eon.py`
- `coordonator.py`

### Domeniu

- Furnizor afectat direct: E.ON Romania.
- Modul comun afectat: coordonatorul, numai pe ramura `cheie_furnizor == "eon"`.
- Alti furnizori afectati: niciunul in mod intentionat.

### Contract de date

- `accessToken` este tokenul trimis in payload-ul `POST /users/v1/userauth/refresh-token`.
- Raspunsul de refresh trebuie sa contina `accessToken`, `access_token` sau `token`.
- `expiresIn` / `expires_in` stabileste programarea urmatorului refresh.
- Tokenul nou si momentul obtinerii se salveaza imediat in config entry.

### Riscuri

- refresh prea devreme, respins cu `6047 Invalid refresh token`;
- doua refresh-uri simultane cu acelasi token;
- token reinnoit in memorie, dar nepersistat in config entry;
- reautentificare dupa restart din cauza varstei pierdute a tokenului.

### Verificari obligatorii

- autentificare initiala cu 2FA;
- sesiune activa minimum 24-48 de ore;
- cel putin doua reinnoiri consecutive fara 2FA;
- restart Home Assistant inainte de expirarea tokenului;
- restart Home Assistant dupa mai multe reinnoiri;
- actualizare manuala E.ON in apropierea momentului de refresh;
- absenta erorii `6047` in jurnal;
- ceilalti furnizori se incarca normal dupa restart.

### Rezultat

- Test nereusit: primul refresh web a fost respins cu `6047 Invalid refresh token`.
- Cauza probabila identificata ulterior in HAR: requestul browserului include `Authorization`, confirmat de preflight si de codul frontend E.ON.

## v1.10.13b6 - grupare e-bloc

- Doua apartamente din aceeasi asociatie trebuie sa apara ca randuri distincte.
- Numarul din antet trebuie sa corespunda randurilor/facturilor agregate.
- Confirmat de utilizator.

## v1.10.13b5 - status Apa Canal Galati

- Conturile multiple folosesc sesiuni separate.
- Statusul facturii se bazeaza pe indicatorul explicit de plata din portal.
- Facturile istorice achitate nu intra in totalul neachitat.
- In asteptarea confirmarii complete.


## v1.10.13b10 - persistenta sesiune E.ON

- [ ] Autentificare E.ON cu MFA functionala pe sesiune dedicata.
- [ ] Cookie-urile E.ON sunt exportate impreuna cu tokenul in config entry.
- [ ] Dupa restart, tokenul si cookie-urile sunt restaurate inainte de primul request.
- [ ] Minimum trei refresh-uri consecutive E.ON functioneaza fara cod 6047.
- [ ] Primul refresh dupa restart HA functioneaza fara reautentificare.
- [ ] Furnizorii non-E.ON continua sa foloseasca aceeasi sesiune ca in versiunea anterioara.
- [ ] Apa Canal Galati isi pastreaza sesiunea dedicata si separarea conturilor.


## Distributie Energie Oltenia - v1.10.14b1
- [ ] Login Keycloak DEO Hub cu credentiale valide.
- [ ] Transfer automat al sesiunii catre Portalul Utilizatorilor.
- [ ] Detectarea tuturor locurilor de consum.
- [ ] Citirea informatiilor contractuale si a grupului de masura.
- [ ] Separarea registrului de consum 1.8.0 de registrul de injectie 2.8.0.
- [ ] Confirmarea functionarii pentru conturi cu mai multe locuri de consum.
- [ ] Confirmarea comportamentului dupa restart Home Assistant.
- [ ] Verificarea ca DEO nu genereaza facturi sau totaluri de plata.

## DEO v1.10.14b2
- Lista locurilor de consum se citeste din variabila JavaScript `let data`, deoarece randurile sunt generate dinamic de `endclient.js`.
- Se pastreaza fallback-ul pentru linkuri cu token deja randate in HTML.
- Tokenul pentru paginile locatiei se regenereaza Base64 din obiectul complet al locului de consum.

## v1.10.14b3 - DEO autentificare Keycloak
- [ ] Login DEO reuseste si cand prima trimitere Keycloak revine la formularul de autentificare.
- [ ] Retry-ul DEO curata doar cookie-urile domeniilor auth/deohub si nu afecteaza alti furnizori.
- [ ] Credentialele DEO gresite sunt raportate ca autentificare esuata dupa maximum doua incercari.

- DEO b4: autentificarea porneste din endpointul aplicatiei `/auth/login`, pentru initializarea corecta a starii OAuth/Keycloak; diagnostic sigur pentru mesajul formularului.

- DEO b5: autentificarea Hub porneste din endpointul Keycloak observat in HAR, cu state/nonce generate local si header Origin: null la POST.

- [ ] DEO b6: diagnosticul Keycloak inregistreaza statusul initial, destinatia redirectului, numele cookie-urilor si prezenta parametrilor formularului fara parola sau valori OAuth complete.

- DEO b7: autentificarea OAuth porneste din pagina protejata `/user/dashboard`, astfel incat DEO Hub sa genereze si sa valideze intern `state` si `nonce`; nu mai sunt construite local.

## DEO v1.10.14b8 - parsare contract si grup masura
- [ ] Campurile din blocurile `left-section` si `right-section` sunt citite ca perechi eticheta-valoare.
- [ ] Serie contor, tip contor, clasa de precizie, data instalarii si periodicitatea citirii au valorile din portal.
- [ ] Furnizorul, tipul si starea locului de consum sunt extrase fara etichete parazite.
- [ ] Nu apar entitati duplicate pentru data instalarii sau starea locului de consum.
- [ ] Indexurile de consum si injectie raman neschimbate.

- [ ] DEO: verificare parser structural pentru tip/serie/clasa contor si tip loc de consum.

- DEO b10: parserul paginii Informatii grup masura citeste explicit lista Contor energie activa si clasele left-data-item-value; verificat sa nu afecteze autentificarea, locurile de consum sau indexurile.

- DEO b11: campurile canonice ale contorului activ inlocuiesc orice antet echivalent din tabelul ascuns; verificat in special `Tip contor`, fara modificarea seriei, clasei, indexurilor sau autentificarii.

- [ ] DEO b12: tipul contorului este extras exclusiv din lista vizibila a contorului activ.
- [ ] DEO b12: istoricul consum/injectie este disponibil ca atribute lunare (maximum 12 luni) si prin senzori agregati.

## DEO - curatare b13

- [ ] Entitatile `Consum ultimele 12 luni` si `Injectie ultimele 12 luni` pastreaza maximum 12 intrari in atributul `istoric_lunar`.
- [ ] Atributele de sumar pentru luna curenta si luna anterioara sunt disponibile si numerice.
- [ ] Atributul tehnic extins `citiri` nu mai este expus in interfata entitatii.
- [ ] Nu mai exista loguri `[DEO DEBUG]` in versiunea clean.


## Release public v1.11.0

- [x] Persistenta sesiunii E.ON pastreaza tokenul si cookie jar-ul intre restarturi.
- [x] Refresh-urile E.ON functioneaza dupa restart fara codul 6047 si fara reautentificare.
- [x] Logurile sensibile `[EON LOCAL TRACE]` au fost eliminate.
- [x] Logurile `[DEO DEBUG]` au fost eliminate.
- [x] Distributie Energie Oltenia expune datele contractuale, contorul, indexurile de consum si injectie si istoricul lunar.
- [x] DEO nu participa la facturi sau totalurile de plata.
- [x] Arhiva publica nu contine `__pycache__` sau fisiere `.pyc`.


## Hotfix public v1.11.1

- [x] Eliminate apelurile ramase catre helperul de diagnostic E.ON `_trace_cookies`.
- [x] Refresh-ul periodic E.ON nu mai esueaza dupa eliminarea logurilor sensibile.
- [x] Persistenta tokenului si a cookie jar-ului E.ON ramane activa.
- [x] Nu sunt modificate fluxurile DEO sau ale altor furnizori.

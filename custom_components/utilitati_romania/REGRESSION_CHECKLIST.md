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

## v1.12.0b1 - Modul Distributie energie

- [ ] Modulul `Distributie energie` este vizibil doar cand exista cel putin o intrare DEO sau DEER disponibila.
- [ ] DEO este detectat prin entitatile `sensor.deo_*` si locurile sunt grupate dupa NLC.
- [ ] DEER este detectat doar pentru entitatile `sensor.hidro_*` cu `tip_serviciu=distributie`, fara a include Hidro Prahova.
- [ ] Sunt afisate consumul, injectia, balanta energetica, ultima citire si datele tehnice disponibile.
- [ ] Istoricul lunar DEO este afisat ca grafic consum versus injectie.
- [ ] Campurile lipsa la DEER sau DEO nu genereaza carduri goale ori erori JavaScript.
- [ ] Modulele Facturi, Indexuri, Setari si Diagnostic raman neschimbate.
- [ ] Afisarea este verificata pe desktop si mobil.


## v1.12.0b2 - Grafic distributie si teme

- [ ] Cardurile modulului folosesc variabilele de tema Home Assistant si au contrast corect in dark/light.
- [ ] Graficul afiseaza cronologic maximum 12 luni disponibile, fara luni inventate.
- [ ] Axa Y afiseaza scara si unitatea kWh, cu linii orizontale de ghidaj.
- [ ] Tooltip-ul afiseaza luna, consumul, injectia si balanta la hover, focus si tap.
- [ ] Selectorul Comparativ/Consum/Injectie actualizeaza scara graficului.
- [ ] Sunt afisate totalurile perioadei si balanta totala.
- [ ] Detaliile locului si ale contorului sunt grupate in sectiuni pliabile.
- [ ] Pe mobil KPI-urile sunt 2x2, graficul poate derula orizontal si tooltip-ul ramane utilizabil.
- [ ] Daca istoricul lipseste, se afiseaza mesaj explicit in locul unui grafic gol.

## v1.12.0b3 - Tema dark, tooltip si stare sectiuni

- [ ] Cardurile modulului Distributie energie folosesc aceeasi paleta dark ca restul dashboard-ului.
- [ ] Tooltip-ul graficului este pozitionat in viewport si ramane complet vizibil pe desktop si mobil.
- [ ] Tooltip-ul functioneaza la hover, focus si tap.
- [ ] Starea sectiunilor Detalii loc de consum si Detalii contor si contract se pastreaza la actualizarile Home Assistant.
- [ ] Actualizarile automate ale entitatilor nu mai inchid imediat sectiunile deschise de utilizator.
- [ ] Graficul si restul modulelor dashboard-ului raman functionale in light si dark.

## v1.12.0b5 - istoric DEER din pagina Informatii POD
- [ ] Datele principale DEER raman disponibile dupa actualizare.
- [ ] Parserul foloseste tabelul `table.infoCP` care contine antetele `Zi citire`, `Registri contor` si `Unitate masura`.
- [ ] Nu se executa niciun request suplimentar pentru istoricul DEER.
- [ ] Registrul 001 produce istoricul lunar de consum.
- [ ] Registrul 002 produce istoricul lunar de injectie.
- [ ] Constanta `1.00000` este interpretata ca 1.
- [ ] Diferentele negative si schimbarile seriei contorului sunt ignorate.
- [ ] Senzorii pentru ultima perioada si ultimele 12 luni primesc valori si atributul `istoric_lunar`.
- [ ] Dashboard-ul DEER afiseaza graficul lunar, fara POD duplicat si cu eticheta `Profil`.


## v1.12.0b6-debug - diagnostic istoric DEER
- [ ] Datele DEER existente raman disponibile dupa actualizare.
- [ ] Logul contine doua linii `[DEER HISTORY DEBUG]` pentru fiecare POD.
- [ ] Se verifica lungimea HTML, numarul tabelelor `infoCP`, prezenta antetelor si numarul randurilor parsate.
- [ ] Nu se efectueaza request suplimentar pentru istoricul DEER.


## v1.12.0b8 - corectie entitati istoric DEER
- [ ] Cele 6 entitati DEER pentru consum/injectie si datele ultimelor citiri sunt create de platforma sensor.
- [ ] Entitatile existente din registry nu mai raman indisponibile dupa restart.
- [ ] Atributele `ultimele_10_indici` se citesc din obiectul curent al coordinatorului, nu din copia initiala a contului.
- [ ] Atributele `istoric_lunar` contin maximum 12 luni pentru consum si injectie.
- [ ] Debugul DEER si profilerul de startup raman active pentru investigatia curenta.


## v1.12.0b9 - task E.ON exclus din bootstrap

- [ ] Home Assistant finalizeaza faza de startup fara sa astepte taskul periodic E.ON.
- [ ] Nu mai apare avertizarea `Setup timed out for bootstrap` pentru `_async_refresh_eon_in_fundal`.
- [ ] Refresh-ul periodic E.ON continua sa ruleze si sa persiste tokenul dupa pornire.
- [ ] Taskul E.ON este anulat corect la descarcarea intrarii.
- [ ] Functionalitatea DEER din v1.12.0b8 ramane neschimbata.
- [ ] Profilerul `UR STARTUP DEBUG` ramane activ pentru investigatiile urmatoare.


## v1.12.0b10 - valori DEER preluate din istoricul curent

- [ ] `Consum ultima perioada` foloseste prima valoare din `istoric_lunar_consum` al contului curent din coordinator.
- [ ] `Consum ultimele 12 luni` este suma istoricului lunar curent, maximum 12 perioade.
- [ ] Datele ultimei citiri folosesc acelasi istoric ca graficul.
- [ ] Pentru locurile fara prosumator, lipsa registrului 002 nu genereaza valori false de injectie.
- [ ] Graficul si cardurile KPI afiseaza aceleasi valori pentru ultima perioada.
- [ ] Fixul E.ON din v1.12.0b9 ramane activ.

## v1.12.0b11 - Asociere distribuitor-furnizor
- Verifica selectorul din Setari pentru fiecare POD/NLC DEO/DEER.
- Verifica persistenta asocierii dupa refresh al paginii.
- Verifica disclaimerul pentru locurile neasociate si butonul Deschide Setari.
- Verifica blocul Furnizor asociat: ultima factura, valoare, scadenta si sold.
- Verifica faptul ca datele distribuitorului raman separate de datele furnizorului.

## v1.12.0b12 - furnizor asociat si estimari prosumator
- [ ] Cardul furnizorului asociat foloseste acelasi fundal ca restul dashboard-ului in tema dark.
- [ ] Pentru un loc prosumator apare separat sectiunea „Energie injectata”.
- [ ] Daca exista factura de injectie asociata, sunt afisate numarul, valoarea si data/scadenta.
- [ ] Pretul mediu de injectie este citit numai din senzorul dedicat prosumatorului.
- [ ] Estimarea perioadei curente foloseste injectia distribuitorului × pretul mediu de injectie.
- [ ] Estimarea istoricului afisat foloseste numai lunile disponibile in grafic.
- [ ] Valorile estimate sunt marcate explicit ca estimari si au disclaimer.
- [ ] Pentru locurile fara prosumator sectiunea de injectie nu apare.
- [ ] Disclaimerul pentru locurile neasociate ramane neschimbat.


## v1.12.0b13 - Asociere persistenta si tema light
- [ ] Asocierile distribuitor-furnizor se salveaza in Home Assistant Store, nu doar in localStorage.
- [ ] Asocierile reapar dupa refresh, restart si din alt browser/dispozitiv.
- [ ] Salvarea nereusita afiseaza mesaj de eroare si nu confirma fals persistenta.
- [ ] Cardul Furnizor asociat si disclaimerul folosesc fundaluri coerente cu tema light.
- [ ] Tema dark ramane neschimbata si cu contrast corect.

## v1.12.0b14 - Persistenta robusta si tema light distributie

- [ ] Asocierile distribuitor-furnizor sunt salvate in optiunile intrarii globale de administrare.
- [ ] Asocierile sunt pastrate si in Store ca fallback si migrare automata.
- [ ] Dupa restart Home Assistant, asocierile sunt restaurate fara dependenta de localStorage.
- [ ] Cardurile KPI, istoricul si detaliile distributiei folosesc paleta deschisa a dashboardului, nu `secondary-background-color` gri.
- [ ] Tema dark pastreaza fundalurile dedicate existente.


## v1.12.0 - Release public

- [ ] Modulul Distributie energie este vizibil doar cand exista DEO sau DEER configurat.
- [ ] Graficele DEO si DEER afiseaza maximum 12 luni, tooltip, scala si totaluri corecte.
- [ ] Locurile fara injectie afiseaza doar consumul disponibil.
- [ ] Asocierea distribuitor-furnizor persista dupa restart Home Assistant.
- [ ] Facturile de consum si injectie apar separat pentru locurile prosumator asociate.
- [ ] Estimarile de injectie sunt marcate explicit ca estimari.
- [ ] Tema light si dark folosesc fundaluri coerente cu restul dashboard-ului.
- [ ] Taskul periodic E.ON nu mai blocheaza finalizarea startup-ului Home Assistant.
- [ ] Logurile de diagnostic `UR STARTUP DEBUG` si `DEER HISTORY DEBUG` nu mai sunt prezente.
- [ ] Arhiva nu contine `__pycache__` sau fisiere `.pyc`.

## v1.12.1 - Republicare HACS

- [ ] Codul functional este identic cu release-ul stabil v1.12.0.
- [ ] `manifest.json`, resursele frontend si cache-busting-ul folosesc versiunea `1.12.1`.
- [ ] Tag-ul `v1.12.1` este creat pe un commit nou, diferit de commitul release-ului v1.12.0.
- [ ] HACS detecteaza versiunea v1.12.1 ca ultima versiune disponibila.
- [ ] Arhiva nu contine `__pycache__` sau fisiere `.pyc`.

## v1.13.0b5 - Retele Electrice Romania

- [ ] Importurile helperilor de naming pentru dispozitivul Retele Electrice se incarca fara `ImportError`.

- [ ] Furnizorul `Retele Electrice Romania` apare in fluxul de configurare si autentificarea foloseste o sesiune dedicata.
- [ ] Un cont valid incarca toate POD-urile fara a expune in log CNP/CUI, parola, cookie-uri sau tokenul Salesforce Aura.
- [ ] Fiecare POD este creat ca dispozitiv separat, cu adresa, stare, tip consumator, serie si tip contor.
- [ ] Istoricul contorului inteligent genereaza maximum 12 luni de consum si injectie din indecsii cumulativi.
- [ ] Diferentele negative si schimbarile seriei contorului nu genereaza consum lunar fals.
- [ ] Pentru citirile din primele trei zile ale lunii, consumul este atribuit lunii anterioare.
- [ ] Locurile fara date de injectie nu afiseaza valori de injectie inventate.
- [ ] Tabul `Distributie energie` detecteaza entitatile `sensor.retele_electrice_*`, afiseaza graficul si permite asocierea cu un furnizor.
- [ ] Asocierea distribuitor-furnizor ramane persistenta dupa restart.
- [ ] DEO si DEER continua sa functioneze si sa fie afisate separat.
- [ ] Versiunea din manifest, backend si frontend este `1.13.0b5`.
- [ ] Arhiva nu contine HAR-uri, credentiale, `__pycache__` sau fisiere `.pyc`.

### Autentificare Retele Electrice b3
- [ ] Config flow foloseste sesiune dedicata cu cookie jar propriu si nu mai apeleaza `close()` pe sesiunea Home Assistant.
- [ ] Prima cerere porneste din `/s/`, urmeaza redirectul real catre formular si foloseste URL-ul final drept Referer.
- [ ] Raspunsul Salesforce `frontdoor.jsp` este identificat si urmat.
- [ ] Configuratia Aura este detectata pentru formele `var auraConfig`, `window.auraConfig` sau `auraConfig`.
- [ ] Logurile `[RETELE ELECTRICE LOGIN DEBUG]` nu contin utilizator, parola, token, SID sau ViewState.

## v1.13.0b5 - autentificare Visualforce Retele Electrice

- [ ] Pagina de login este incarcata din URL-ul canonic cu `ec=302`.
- [ ] Payload-ul contine formularul, username, password, ViewState, ViewStateVersion, ViewStateMAC si `loginPage:loginForm:j_id25`.
- [ ] Cererea POST foloseste antetele de navigare observate in browser.
- [ ] URL-ul `frontdoor.jsp` este detectat si urmat fara expunerea SID-ului in log.
- [ ] Logurile de diagnostic nu contin credentiale sau tokenuri.
- [ ] Versiunea din manifest, backend si frontend este `1.13.0b5`.


## v1.13.0b7 - submit Visualforce confirmat pentru Retele Electrice

- [ ] Formularul `loginPage:loginForm` adauga explicit `loginPage:loginForm:j_id25` cand butonul apeleaza `logintest()`.
- [ ] Autentificarea nu mai retrimite pagina de login din cauza lipsei comenzii JSF.
- [ ] Raspunsul de autentificare contine sau conduce catre `frontdoor.jsp`.
- [ ] Configuratia Aura este incarcata dupa autentificare.
- [ ] Logurile de diagnostic nu contin parola, ViewState, SID sau tokenuri.
- [ ] Versiunea din manifest, backend si frontend este `1.13.0b7`.


## v1.13.0b7 - istoric lunar Retele Electrice

- [ ] Diferentele dintre toate citirile valide din aceeasi luna sunt insumate, nu deduplicate.
- [ ] Citirile sunt calculate separat pentru fiecare serie de contor.
- [ ] La demontarea contorului, intervalul final al contorului vechi este atribuit lunii anterioare.
- [ ] Lunile calendaristice fara o diferenta calculabila apar in istoric ca date indisponibile, nu sunt sarite.
- [ ] Pentru POD-ul de test, consumul iunie 2026 este 462 kWh si totalul ultimelor 12 luni este 4723 kWh.
- [ ] Versiunea din manifest, backend si frontend este `1.13.0b7`.


## v1.13.0 - Retele Electrice Romania

- [ ] Furnizorul `Retele Electrice Romania` poate fi adaugat cu datele contului din portal.
- [ ] Autentificarea Visualforce urmeaza `frontdoor.jsp`, initializeaza sesiunea Aura si nu expune credentiale sau tokenuri.
- [ ] Fiecare POD este creat ca dispozitiv separat, cu adresa, stare, tip loc, contor si date contractuale.
- [ ] Istoricul lunar afiseaza maximum 12 luni consecutive, fara luni sarite artificial.
- [ ] Diferentele dintre citiri sunt insumate in luna corecta si sunt calculate separat pentru fiecare serie de contor.
- [ ] Schimbarea contorului nu produce diferente negative si nu amesteca indecsii contoarelor.
- [ ] Pentru POD-ul testat, iunie 2026 este 462 kWh, iar totalul ultimelor 12 luni este 4723 kWh.
- [ ] Locurile neprosumatoare nu afiseaza valori de injectie inventate.
- [ ] Tabul `Distributie energie` afiseaza Retele Electrice si permite asocierea persistenta cu furnizorul.
- [ ] DEO si DEER continua sa functioneze fara regresii.
- [ ] Logurile temporare `[RETELE ELECTRICE LOGIN DEBUG]` nu mai sunt prezente.
- [ ] Versiunea din manifest, backend si frontend este `1.13.0`.
- [ ] Arhiva nu contine HAR-uri, credentiale, `__pycache__` sau fisiere `.pyc`.


## v1.13.1b1 - date instantanee contor Retele Electrice

- [ ] Butoanele `Solicită actualizare contor` si `Încarcă date contor` apar numai pentru locurile cu contor inteligent.
- [ ] `Solicită actualizare contor` trimite actiunea `ReqMeterInstantData` si afiseaza timpul estimat comunicat de portal.
- [ ] `Încarcă date contor` foloseste actiunea `FindOutMeterInstantData` si actualizeaza entitatile fara restart sau reload al integrarii.
- [ ] Sunt disponibile indexurile instant 1.8.0 si 2.8.0, energiile reactive, tensiunile, curentii si puterea instantanee.
- [ ] Pentru un loc neprosumator nu sunt create entitatile de injectie.
- [ ] Datele instantanee apar si in tabul `Distributie energie`, impreuna cu cele doua actiuni.
- [ ] Valorile istorice lunare si indexurile oficiale existente nu sunt suprascrise cu citirea instantanee.
- [ ] Un esec sau o valoare inca indisponibila produce un mesaj clar, fara a bloca refresh-ul obisnuit.
- [ ] Versiunea din manifest, backend si frontend este `1.13.1b1`.

## v1.13.1b2 - actualizare automata si intreruperi Retele Electrice

- [ ] Versiunea din manifest, backend si frontend este `1.13.1b2`.
- [ ] La `Actualizeaza contor`, integrarea memoreaza valoarea `LAST_UPDATED` existenta inainte de solicitare.
- [ ] `Incarca date` nu accepta drept succes aceeasi valoare `LAST_UPDATED`; afiseaza ca portalul returneaza inca datele anterioare.
- [ ] Dupa aparitia unei citiri noi, `Data si ora citire` afiseaza data si ora completa.
- [ ] Dashboard-ul formateaza `Ultima actualizare raportata de contor` cu data si ora locala.
- [ ] In Setari apare campul `Actualizare automata contor` pentru intrarile Retele Electrice.
- [ ] Valoarea 0 dezactiveaza actualizarea automata; valorile 1-24 pornesc ciclul la intervalul ales.
- [ ] Intervalul este salvat in options/config entry si persista dupa restart Home Assistant.
- [ ] Pentru mai multe POD-uri cu smart meter, cererile sunt lansate inainte de asteptare, apoi datele sunt incarcate pentru fiecare POD.
- [ ] Actualizarea automata asteapta estimarea portalului si reincearca daca `LAST_UPDATED` nu s-a schimbat.
- [ ] Textul despre starea alimentarii este preluat din fluxul `PowerOutages`.
- [ ] Mesajul `nu avem inregistrata nicio intrerupere` este afisat informativ si nu activeaza binary sensorul de problema.
- [ ] Un mesaj real despre avarie/intrerupere activeaza binary sensorul si bannerul de avertizare.
- [ ] Incarcarea manuala si automata a datelor contorului actualizeaza si starea intreruperilor.
- [ ] Disclaimerul pentru asocierea distribuitor-furnizor este lizibil in tema dark.
- [ ] Tema light ramane coerenta pentru disclaimer, valori instantanee si bannerul de alimentare.
- [ ] Arhiva nu contine HAR-uri, credentiale, `__pycache__` sau fisiere `.pyc`.


## v1.13.1b3 - incarcare valori existente in timpul asteptarii

- [ ] Versiunea din manifest, backend si frontend este `1.13.1b3`.
- [ ] La pornire, un contor inteligent incarca ultimele valori instantanee disponibile si senzorii nu raman `unknown`.
- [ ] Dupa `Solicita actualizare contor`, apasarea imediata pe `Incarca date contor` incarca valorile vechi disponibile.
- [ ] Cand noile valori nu sunt inca disponibile, notificarea precizeaza ca au fost incarcate datele anterioare si afiseaza data si ora lor.
- [ ] Dashboard-ul afiseaza persistent starea de asteptare si timestampul valorilor incarcate.
- [ ] Actualizarea automata continua sa reincearce pana la 4 ori pentru date noi, dar pastreaza ultimele valori disponibile daca portalul nu s-a actualizat.
- [ ] Cand portalul returneaza un timestamp nou, starea se schimba in `actualizate`.

## v1.13.1 - date instantanee, actualizare automata si stare alimentare Retele Electrice

- [ ] Versiunea din manifest, backend si frontend este `1.13.1`.
- [ ] Butoanele `Solicita actualizare contor` si `Incarca date contor` apar numai pentru locurile cu contor inteligent.
- [ ] Solicitarea actualizarii transmite cererea catre contor si afiseaza timpul estimat comunicat de portal.
- [ ] Incarcarea datelor actualizeaza indexurile instantanee, energiile reactive, tensiunile, curentii, puterea si data/ora citirii.
- [ ] Daca citirea noua nu este inca disponibila, sunt pastrate si afisate ultimele valori disponibile impreuna cu timestampul lor.
- [ ] La pornire, ultimele valori disponibile sunt incarcate in fundal fara sa blocheze finalizarea pornirii Home Assistant.
- [ ] Intervalul de actualizare automata poate fi configurat intre 0 si 24 de ore si persista dupa restart.
- [ ] Actualizarea automata solicita mai intai citirea, asteapta intervalul portalului si reincearca preluarea fara a pierde datele vechi.
- [ ] Mesajul despre intreruperile de alimentare este preluat din portal si actualizeaza bannerul si binary sensorul dedicat.
- [ ] Lipsa unei intreruperi este afisata informativ si nu activeaza binary sensorul de problema.
- [ ] Disclaimerul pentru asocierea distribuitor-furnizor este lizibil in temele light si dark.
- [ ] Datele istorice si indexurile oficiale nu sunt suprascrise de valorile instantanee.
- [ ] DEO, DEER si celelalte platforme continua sa functioneze fara regresii.
- [ ] Arhiva nu contine HAR-uri, credentiale, loguri temporare, `__pycache__` sau fisiere `.pyc`.

## v1.15.2b1 - Apa Nova Bucuresti

- [ ] Furnizorul `Apa Nova Bucuresti` apare in config flow si poate fi adaugat cu emailul si parola contului mobil.
- [ ] Autentificarea obtine mai intai tokenul tehnic al aplicatiei, apoi tokenul utilizatorului, fara sa scrie credentiale sau tokenuri in log.
- [ ] Lista codurilor de client este citita din `GetCodClientListByToken`, iar fiecare cod este procesat separat.
- [ ] Punctele de consum cu contor activ sunt create ca dispozitive distincte, folosind codul punctului de consum drept identificator stabil.
- [ ] Punctele istorice fara contor nu genereaza entitati goale sau duplicate.
- [ ] Soldul total este preluat din `apiclientsold` si coincide cu valoarea afisata in aplicatie.
- [ ] Istoricul facturilor este preluat din `apiclientinvoices`; factura achitata are sold zero si status `achitata`.
- [ ] Facturile neachitate sunt identificate din soldul individual si nu doar din textul statusului.
- [ ] Ultima factura, urmatoarea scadenta, numarul facturilor si numarul facturilor neachitate sunt afisate corect.
- [ ] Ultimul index, data citirii, seria contorului si starea smart sunt preluate din `apiclientcheckmeterautoreading`.
- [ ] Istoricul indexurilor si consumul lunar sunt preluate din `apiclientindexhistory`.
- [ ] Pentru contoarele smart, citirea manuala nu este marcata ca disponibila.
- [ ] Endpointul de descarcare factura ramane documentat: PDF-ul este Base64 in `content.InvoiceContent`; integrarea nu logheaza continutul PDF.
- [ ] Diagnostics nu expune parola, tokenurile de acces, tokenul de refresh sau credentialele tehnice ale aplicatiei.
- [ ] Versiunea din manifest, backend si frontend este `1.15.2b1`.
- [ ] Arhiva nu contine HAR-uri, credentiale de utilizator, tokenuri capturate, `__pycache__` sau fisiere `.pyc`.


## v1.15.2b2 - plati si curatare entitati Apa Nova Bucuresti

- [ ] Endpointul `apiclientpayments/{client_number}` este incarcat pentru fiecare cod de client.
- [ ] `Numar plati` afiseaza numarul real al documentelor de plata returnate de API.
- [ ] `Data ultimei plati` este cea mai recenta data valida din istoricul platilor.
- [ ] `Valoare ultima plata` insumeaza toate documentele din cea mai recenta zi de plata.
- [ ] Atributele ultimei plati contin metodele si numerele documentelor individuale.
- [ ] `Scadenta ultimei facturi` foloseste scadenta celei mai recente facturi, nu cea mai veche restanta.
- [ ] `Sold curent` nu mai dubleaza senzorul `De plata`.
- [ ] `Factura restanta` este binary sensor de tip problem.
- [ ] `Contor inteligent` este binary sensor si nu mai afiseaza valoarea textuala `True`.
- [ ] Senzorii vechi `sensor.*_factura_restanta`, `sensor.*_contor_inteligent` si `sensor.*_sold_curent` nu mai sunt creati pentru Apa Nova Bucuresti.
- [ ] Versiunea din manifest, backend si frontend este `1.15.2b2`.
- [ ] Arhiva nu contine HAR-uri, credentiale de utilizator, tokenuri capturate, `__pycache__` sau fisiere `.pyc`.


## v1.15.2b4 - facturi neachitate individuale Apa Nova Bucuresti

- [ ] Pentru Apa Nova Bucuresti, fiecare factura neachitata este afisata ca rand/card separat in dashboard.
- [ ] Numarul de facturi neachitate din dashboard este egal cu lista returnata de `apiclientunpaidinvoices`.
- [ ] Totalul neplatit se calculeaza din soldul individual al fiecarei facturi (`sold_factura`/`Sold`), fara multiplicarea soldului total al contului.
- [ ] Pentru datele de test, sunt afisate 3 facturi neachitate, iar totalul este 640,12 RON.
- [ ] Facturile platite raman agregate conform comportamentului existent si nu dubleaza cardurile.
- [ ] Versiunea din manifest, backend si frontend este `1.15.2b4`.


## v1.15.2b7 - Apa Nova: afișare facturi în dashboard

- Baza modificării este v1.15.2b4, deoarece aceasta păstra corect toate facturile și totalul neachitat.
- Dacă există facturi neachitate, dashboardul afișează exclusiv toate facturile neachitate.
- Facturile sunt clasificate folosind `date_brute.sold_factura` și `FacturaUtilitate.stare`, nu atribute inexistente pe model.
- Dacă nu există restanțe, dashboardul afișează numai ultima factură.
- Totalul de plată rămâne suma soldurilor individuale ale tuturor facturilor neachitate; pentru contul de test: 640,12 RON.
- Caz de regresie: 3 facturi neachitate + 1 factură plătită trebuie să producă 3 carduri, 0 carduri plătite și total 640,12 RON.


## v1.16.0 - Apa Nova Bucuresti

- [ ] Furnizorul Apa Nova Bucuresti poate fi configurat cu email si parola.
- [ ] Sunt procesate toate codurile de client asociate contului.
- [ ] Soldul total, facturile, PDF-urile, punctele de consum, indexurile si platile sunt disponibile.
- [ ] Daca exista facturi neachitate, dashboardul afiseaza toate facturile neachitate si nu afiseaza facturi platite.
- [ ] Daca nu exista facturi neachitate, dashboardul afiseaza doar cea mai recenta factura.
- [ ] Totalul neachitat este calculat din soldurile reale ale facturilor si corespunde soldului contului.
- [ ] Pentru contul de test sunt afisate 3 facturi neachitate, cu total 640,12 RON.
- [ ] Entitatile de plati folosesc ultima zi de plata, iar valoarea reprezinta suma documentelor din acea zi.
- [ ] Versiunea din manifest, backend si frontend este `1.16.0`.


## v1.16.1b1 local - Separare sigură date administrative

- [ ] Senzorul `sensor.administrare_integrare_facturi_utilitati` nu mai publică `locuri_consum` și `locuri_consum_ignorate`.
- [ ] Noul senzor `sensor.administrare_integrare_locuri_consum_utilitati` publică ambele liste administrative.
- [ ] Dashboardul Facturi, Prezentare și Scadențe păstrează toate datele și acțiunile existente.
- [ ] Setări afișează toate locurile de consum, inclusiv locațiile fără facturi și cele ignorate.
- [ ] Ignorarea, reactivarea și redenumirea locațiilor funcționează neschimbat.
- [ ] Frontendul păstrează fallback pentru instalările care încă au listele în senzorul vechi.
- [ ] Logul local confirmă că fiecare entitate rămâne sub limita de 16.384 bytes.
- [ ] Versiunea din manifest, backend și frontend este `1.16.1b1`.


## v1.16.1b2 local - Compatibilitate entități și eliminare loguri

- [ ] Senzorul agregat de facturi rămâne sub limita Home Assistant de 16.384 bytes după încărcarea tuturor furnizorilor.
- [ ] Senzorul separat pentru administrarea locurilor de consum păstrează ignorarea, reactivarea și redenumirea locațiilor.
- [ ] Nu mai apar mesajele `[AGREGARE LOCAL DIAG]` și `[AGREGARE LOCAL IMPACT]` în jurnal.
- [ ] Entitățile noi Rețele Electrice folosesc exclusiv `entity_id` lowercase pentru POD.
- [ ] Entitățile Rețele Electrice existente sunt migrate în entity registry la varianta lowercase, cu `unique_id` neschimbat.
- [ ] Senzorii și butoanele Rețele Electrice rămân disponibili și funcționali după restart.
- [ ] Nu mai apare avertismentul Home Assistant privind entity ID invalid pentru `sensor.retele_electrice_*` și `button.retele_electrice_*`.
- [ ] Versiunea din manifest, backend și frontend este `1.16.1b2`.


## v1.16.1b3 local - Payload facturi prin WebSocket

- [ ] Senzorul `sensor.administrare_integrare_facturi_utilitati` nu mai expune atributul voluminos `locatii`.
- [ ] Sumarul numeric al facturilor rămâne disponibil în atributele senzorului.
- [ ] Panoul principal încarcă structura completă prin `utilitati_romania/dashboard_payload`.
- [ ] Cardul Lovelace încarcă aceeași structură prin WebSocket.
- [ ] Există fallback către atributul vechi `locatii` pentru compatibilitate.
- [ ] Taburile Prezentare, Facturi, Indexuri și Setări se afișează corect.
- [ ] Gruparea pe locație/furnizor, filtrarea, PDF și actualizarea manuală funcționează.
- [ ] Senzorul separat pentru locuri de consum rămâne funcțional.
- [ ] ID-urile Rețele Electrice rămân lowercase și stabile.
- [ ] Logurile locale confirmă că atributele senzorului principal rămân sub 16 KB.
- [ ] Versiunea din manifest, backend și frontend este `1.16.1b3`.

## v1.16.1 - Compatibilitate Recorder și Rețele Electrice

- [ ] Payloadul complet al facturilor este furnizat panoului și cardului prin WebSocket, fără atributul voluminos `locatii` în senzorul agregat.
- [ ] `sensor.administrare_integrare_facturi_utilitati` rămâne sub limita Recorder de 16384 bytes și păstrează doar sumarul.
- [ ] `sensor.administrare_integrare_locuri_consum_utilitati` furnizează lista administrativă și locațiile ignorate pentru Setări.
- [ ] Panoul Prezentare afișează totalurile, scadențele și sumarul pe locații.
- [ ] Tabul Facturi afișează facturile, stările, PDF-ul și actualizarea manuală.
- [ ] Tabul Setări permite ignorarea și reactivarea locurilor de consum.
- [ ] Cardul Lovelace preia payloadul prin WebSocket și păstrează fallback-ul compatibil.
- [ ] ID-urile entităților Rețele Electrice sunt normalizate lowercase, fără modificarea `unique_id`, POD-ului sau apelurilor API.
- [ ] Nu mai apar avertismente pentru ID-uri invalide Rețele Electrice.
- [ ] Nu mai apar logurile locale `[AGREGARE LOCAL DIAG]` și `[AGREGARE LOCAL ADMIN]`.
- [ ] Versiunea din manifest, backend și frontend este `1.16.1`.



## v1.16.2b1 local - Diagnostic Orange și Hidroelectrica

- [ ] Beta pornește fără a bloca inițializarea Home Assistant.
- [ ] Nu schimbă logica facturilor sau totalurilor; adaugă doar diagnostic.
- [ ] Logurile `[ORANGE AGG TRACE]` și `[ORANGE DASH TRACE]` apar după actualizare.
- [ ] Logurile `[HIDRO AGG TRACE]` și `[HIDRO DASH TRACE]` apar după actualizare.
- [ ] Logurile nu conțin tokenuri, parole, adrese complete sau identificatori compleți.
- [ ] Dashboardul, PDF-urile și actualizarea manuală funcționează identic cu v1.16.1.
- [ ] Versiunea este sincronizată la `1.16.2b1`.


## v1.16.2b2 - Orange / Hidroelectrica
- [ ] Orange: soldul comun al serviciilor nu este multiplicat pe fiecare abonament.
- [ ] Orange: ratele sunt afisate separat, iar totalul agregat coincide cu totalul profilului.
- [ ] Hidroelectrica: soldul curent este asociat facturii indicate de bill_id, nu documentelor istorice cu scadenta indepartata.
- [ ] Logurile ORANGE/HIDRO TRACE confirma randurile si totalurile finale.

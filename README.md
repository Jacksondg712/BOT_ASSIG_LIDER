# BOT_ASSIG_LIDER

Asigna automaticamente el owner de **Leads nuevos** del segmento del reporte
**"Copia de Leads-Created-B2B_All-Unassigne"** a un owner con un **titulo de
lider configurable**.

Replica el esquema de `BOT_ASSIGNMENTS/BOT_SLACK_SALESFORCE` (mismo patron de
cliente Salesforce, config via `.env` + JSON editable, resultados
estructurados). La diferencia esta en el disparo: no reacciona a un mensaje
de Slack ni (por ahora) a una notificacion de Salesforce — **consulta
Salesforce cada cierto intervalo (polling)**, porque hoy no hay permisos de
admin en el org para crear los Named/External Credentials que un Flow
necesitaria para avisar al bot en tiempo real. Ver "Modo Flow (para mas
adelante)" al final si eso cambia.

## Estado actual (importante)

1. **Credenciales**: corregidas y verificadas (2026-09-08). Login SOAP
   exitoso contra `https://openeducation.my.salesforce.com`.
2. **Criterio del segmento**: identificado y ya implementado. Ver
   [lead_segment.json](lead_segment.json) y la seccion "Criterio real del
   segmento" mas abajo.
3. **Modo de disparo**: **polling**, cada `POLL_INTERVAL_SECONDS` (default
   60s). Arranca solo al levantar la app (`uvicorn main:app`), no requiere
   ngrok ni URL publica — el bot solo hace llamadas salientes a Salesforce.
4. **Reparto por equipo (pais → Title)**: ya NO hay owner fijo
   (`LEADER_TEST_OWNER_EMAIL`/`NAME` se eliminaron). Ver
   [team_routing.json](team_routing.json) y la seccion "Reparto por pais"
   mas abajo.
5. **Backlog existente**: hay **5646 Leads B2B Company** que hoy estan en
   manos del usuario de integracion (sin asignar a un vendedor) segun el
   mismo criterio. El poller **NO los toca**: al arrancar por primera vez
   guarda un checkpoint en `poll_state.json` con la fecha/hora de arranque,
   y solo procesa Leads creados despues de esa fecha. Ver "Procesar el
   backlog existente" mas abajo si en algun momento quieren limpiarlo.
6. **MODO PRUEBA ACTIVO (2026-09-15)**: `lead_segment.json` tiene
   `"utm_source": "test"` — el bot **solo** procesa leads con
   `utmSource__c = 'test'` (los del reporte "Copy
   Leads-Created-B2B_All-Unassigned_TE", Id `00OWP000005WQSj2AO`), y
   **no toca ningun lead real de produccion** mientras esto este activo.
   Poner ese campo en `""` (o borrar la linea) en `lead_segment.json` cuando
   se quiera cubrir todo el trafico real.

## Criterio real del segmento (identificado via API, 2026-09-08)

El "reporte" que mencionaste (`00OWP000003xuHN2AY`, nombre real: es un
**Report** de Salesforce) tiene 13 filtros combinados con AND. Los 3 que
definen el "unassigned" real en este org (los demas son exclusiones de
calidad de dato: emails/companies de test, paises restringidos, campañas de
eventos, etc.) son:

| Campo | Condicion | Valor real |
|---|---|---|
| RecordType | igual a | **B2B Company** (`RecordTypeId = 01231000000xzVuAAI`, DeveloperName `B2B_Company`) |
| Owner | es uno de los "sin asignar de verdad" | ver "Ojo con Brasil" abajo — **NO es un solo owner** |
| Status | distinto de | `Dead lead - Closed`, `Dead Lead` |

El filtro de fecha del reporte (`CreatedDate >= 2026-04-01`) es solo un
ancla historica del reporte original, no algo que haya que replicar — el
poller usa su propio checkpoint (ver arriba) para lo mismo.

### Ojo con Brasil: "sin asignar" no es un solo owner (descubierto 2026-09-15)

La mayoria de paises dejan el lead en manos de **Salesforce Integration
API** (`Id 0050Z000008e7RPQAY`, username `integrationapi@openenglish.com`,
el usuario que lo crea via la API de captura) hasta que alguien lo asigna a
mano — eso es lo que pensamos originalmente que significaba "sin asignar"
en todo el org.

Pero **Brasil tiene su propia asignacion automatica en Salesforce** que
manda el lead directo a la Queue **"Sales Leads B2B Brazil"**
(`Id 00G0Z000004wNn2UAE`) apenas se crea — nunca pasa por el usuario de
integracion. Esto se descubrio probando con un lead de prueba real de
Brasil que el bot no detectaba (quedaba silenciosamente fuera del
segmento, no daba error).

Por eso `lead_segment.json` tiene una **lista** `unassigned_owners` (no un
solo campo), con entradas `{"type": "User"|"Queue", "identifier": "..."}`.
Si en el futuro aparece OTRO pais con su propia Queue de asignacion
automatica, se agrega ahi una entrada mas — sin tocar codigo. El poller
resuelve cada entrada a su Id real (Usuario o Queue) via
`_resolve_unassigned_owner_ids()` en `services/poller.py`, y la validacion
de segmento (`_owner_is_unassigned()` en `services/lead_assignment.py`)
usa la misma lista.

Esta reflejado en [lead_segment.json](lead_segment.json), editable sin tocar
codigo. `services/salesforce_client.py` resuelve `RecordType Id` y `User Id`
dinamicamente a partir de esos nombres (con cache en memoria), asi que si
algun dia cambian de Id (poco probable) no hay que tocar nada.

**Importante**: el bot nunca consulta un Report por su Id — ni el original
(`00OWP000003xuHN2AY`) ni la copia de pruebas
`Copy Leads-Created-B2B_All-Unassigned_TE` (`00OWP000005WQSj2AO`). Consulta
Leads directamente via SOQL con el criterio de arriba. La copia de pruebas
solo se usa para aislar leads de prueba con un filtro extra
(`utmSource__c = 'test'`), reflejado en `lead_segment.json` como
`utm_source` — ver "MODO PRUEBA ACTIVO" mas arriba.


### Cuando Salesforce cambia el usuario de integracion (2026-10-14)

El 8 oct apareció un usuario **nuevo** `Store Integration`
(`store.integration@openenglish.com`, id `005WP00000LD1VFYA1`), distinto de
`Salesforce Integration API` (que sigue existiendo). Los leads que quedaban con
ese Owner no coincidian con `unassigned_owners` y eran **invisibles** para el
bot — sin error, sin log. Ya esta agregado en `lead_segment.json`.

**Regla general:** cada vez que Salesforce agregue/cambie un usuario o cola de
integracion, hay que agregarlo en dos lados: el filtro `OWNER` del reporte
original y `unassigned_owners` en `lead_segment.json`.
`tests/test_lead_segment_config.py` valida la lista real. Los leads que se
quedaron con el Owner desconocido *antes* de agregarlo se recogen solos si se
crearon dentro de las ultimas `POLL_LOOKBACK_HOURS` horas (ver "Revision hacia
atras"). Los mas viejos hay que procesarlos a mano con `/salesforce/lead-created`.

## Reparto por pais (equipo Brasil vs equipo Latam)

Desde el 2026-09-15, el bot enruta el lead segun `Country_of_Residence__c`,
no a un owner fijo:

1. `_target_titles_for_country()` en
   [services/lead_assignment.py](services/lead_assignment.py) decide la
   **lista de Titles** que trabajan el lead, usando
   [team_routing.json](team_routing.json):

   | Pais del lead | Titles del pool |
   |---|---|
   | contiene `brazil`/`brasil` | `brasil_titles`: `(BR)` |
   | contiene `usa`/`united states`/`estados unidos`/... | `usa_titles`: **solo** `(LT) (US)` |
   | cualquier otro (Colombia, Venezuela, Chile, Nicaragua...) | `default_titles`: `(LT)` **+** `(LT) (US)` juntos |

   Es decir: los `(LT) (US)` son los **unicos** que reciben USA, pero
   **tambien** entran al reparto equitativo de Latam junto con los `(LT)`.
2. `salesforce.find_active_users_by_titles(titles)` trae **todos** los
   `User` activos cuyo `Title` sea cualquiera de esa lista (`Title IN (...)`).
3. `_pick_next_owner()` (alias de `services/round_robin.py::pick_next`)
   elige a cual le toca el lead — ver "Reparto equitativo" abajo. La
   rotacion se lleva **por pool** (la clave es la lista de titles unida), asi
   que Latam rota entre (LT)+(US) y USA/Brasil tienen su propia rotacion.
4. `_validate_owner_title()` confirma que el usuario elegido tiene alguno de
   los Titles esperados (defensa en profundidad).

**Por que "contiene" y no igualdad exacta para Brasil**: el picklist de
Salesforce tiene el valor en español (`"Brasil"`), pero los Leads que llegan
por la API de captura traen el pais en texto libre e inconsistente (ej.
`"Brazil"`, `"São Paulo, Brazil"`). Ver el comentario en
`team_routing.json`.

Editar `team_routing.json` para agregar paises/equipos nuevos sin tocar
codigo (ej. un tercer equipo para otro pais especifico). Si algun equipo no
tiene ningun usuario activo con ese Title, el lead queda `action=error` con
el motivo explicito — nunca se asigna a ciegas ni cae a un owner por
default.

## Reparto equitativo (round-robin) cuando hay varios owners por equipo

[services/round_robin.py](services/round_robin.py) — cuando
`find_active_users_by_title()` devuelve mas de un usuario activo con el
mismo Title, se turnan en orden estable por `Id` (no por `Name` ni
`LastModifiedDate`, que pueden cambiar y desordenar la rotacion): al primer
lead le toca el primero de la lista, al siguiente el segundo, y asi
sucesivamente, volviendo al principio al llegar al final.

- El puntero de "a quien le toco la ultima vez" se guarda en
  `round_robin_state.json` (uno por Title), para que la rotacion **sobreviva
  reinicios del bot** — igual que el checkpoint del poller en
  `poll_state.json`. No se versiona en git (ver `.gitignore`).
- Si el owner que tenia el turno ya no aparece entre los activos con ese
  Title (se desactivo, le cambiaron el equipo), la rotacion de ese equipo se
  retoma desde el primero — no se rompe ni requiere intervencion manual.
- Con un solo owner activo en el equipo, siempre se le asigna a esa persona
  (no se toca el archivo de estado).
- Si se necesita reparto por **menor carga** en vez de round-robin puro, el
  unico punto de codigo a tocar es `pick_next()` en
  `services/round_robin.py` — la llamada desde `services/lead_assignment.py`
  no cambia.

## Asistencia diaria (solo reparte entre los conectados)

[services/attendance.py](services/attendance.py) +
[services/attendance_report.py](services/attendance_report.py) +
[attendance.json](attendance.json). Todo ocurre solo en **dias habiles de
Colombia**: lunes a viernes, sin festivos (los calcula la libreria
`holidays`, incluye los trasladados a lunes por la Ley Emiliani). En fines de
semana y festivos no se filtra ni se envia nada.

Antes del round-robin, en pools con **2 o mas personas** se descarta a quien
no este conectado hoy, segun el `LoginHistory` de Salesforce:

- **Tomas de asistencia**: `07:00`, `08:00` y `13:00` hora Bogota
  (`snapshot_times`). Desde la toma de las 13:00 tambien entra al reparto
  quien se conecto tarde en la mañana.
  En cada toma esta presente quien tenga un login valido entre las 00:00 del
  dia y esa hora **mas el margen** (`grace_minutes`, hoy **30**, para quien
  tenga alguna dificultad para conectarse: quien entre hasta las 8:30 cuenta
  para la toma de las 8:00). Un lead que llega entre dos tomas usa la ultima
  cumplida. No hay scheduler ni estado para el filtro: `LoginHistory` es
  historico, asi que el resultado se calcula (y cachea) cuando llega el lead.
- **Que cuenta como "conectado"**: solo logins de navegador
  (`counted_applications: ["Browser"]`). Slack y la app movil se ignoran porque
  registran logins automaticos a cualquier hora (ej. 00:48 am).
- **Pool de 1 sola persona** (USA, Brasil hoy): se le asigna **siempre**, este
  conectada o no.
- **Nadie conectado** en un pool de 2+: no se filtra, se reparte entre todos.
- **Antes de la primera toma** del dia (ej. 3 am): no se filtra.
- **Falla abierta**: si la consulta a Salesforce falla, no se filtra.
- El motivo queda en el detalle del post de cada asignacion
  (`Asistencia 08:00: 1 de 2 conectados (ausente: ...)`) y en los logs
  `[ASISTENCIA ...]`. `enabled: false` apaga todo.

### Mensaje de asistencia en Slack

Dos veces por dia habil, cuando se cierra el margen de cada toma (08:00 + 30
min = **8:30**, y 13:00 + 30 min = **1:30 pm**), el bot publica la asistencia
por equipo (Brasil, USA, Latam): quien esta conectado y desde que hora, quien
no, y que regla de reparto aplica.

- Tomas que generan mensaje: `report.snapshots` (hoy `["08:00", "13:00"]`;
  agrega `"07:00"` para recibir tambien uno a las 7:30). Cada una debe estar
  tambien en `snapshot_times`, para que el mensaje y el reparto usen la misma
  lista de conectados.
- Ojo: `LoginHistory` solo registra entradas, no salidas. En la toma de la 1
  pm cuenta como conectado quien entro en cualquier momento desde las 00:00,
  aunque ya se haya ido.
- Va al canal `ATTENDANCE_CHANNEL_ID` del `.env`; si esta vacio, al mismo
  canal de asignaciones (`LEAD_ASSIGNMENT_CHANNEL_ID` / `SLACK_TEST_CHANNEL_ID`).
- **No se duplica** si el bot se reinicia (`attendance_report_state.json`, no
  se versiona). Si el bot estaba apagado a las 8:30 y arranca despues, lo envia
  igual siempre que no hayan pasado mas de `report.catchup_minutes` (120); pasado
  eso lo omite (un reporte de las 8:30 a las 5 pm no sirve).
- Si el envio falla, se reintenta en el siguiente ciclo (cada minuto).

Requiere que el usuario de integracion pueda leer `LoginHistory` (ya
verificado). Ojo: Salesforce no deja filtrar `LoginHistory.Status` en el
`WHERE`, por eso se filtra en Python.

## Notificaciones de Slack (DM al owner + registro en canal)

App de Slack propia (no comparte token con `BOT_SLACK_SALESFORCE`). Bot
Token Scopes usados: `chat:write`, `im:write`, `users:read`,
`users:read.email`.

- **`.env`**: `SLACK_BOT_TOKEN` (`xoxb-...`), `SLACK_NOTIFICATIONS_ENABLED`,
  `LEAD_ASSIGNMENT_CHANNEL_ID` (o cae a `SLACK_TEST_CHANNEL_ID`). El bot debe
  estar invitado al canal (`/invite @Bot Asig Lider`).
- **No requiere ngrok**: el bot solo hace llamadas SALIENTES a la API de
  Slack (buscar usuario, abrir DM, postear) — nadie desde afuera necesita
  llegar al bot. Un servidor local (`python -m uvicorn ...`) con salida a
  internet es suficiente, igual que con el polling de Salesforce.
- Se dispara justo despues de escribir el `OwnerId` en Salesforce (nunca en
  el caso "ya pertenecia" ni si hubo error) desde
  `services/lead_assignment.py::_notify_assignment`. Un fallo de Slack se
  loguea pero **nunca** revierte ni marca como error la asignacion ya hecha
  en Salesforce.
- Texto predeterminado editable en
  [utils/slack_formatter.py](utils/slack_formatter.py): DM al owner con el
  link al Lead, y un post en el canal con header, link, a quien se asigno
  (con `@mencion` si se encuentra su usuario de Slack por email) y el motivo
  de la validacion.
- **Zona horaria fija: Bogota** (todos los SDR estan ahi) — las fechas de
  los mensajes (`_now_label()` en `slack_formatter.py`) siempre se muestran
  en `America/Bogota`, sin importar en que servidor/zona horaria corra el
  bot. Requiere el paquete `tzdata` (ya en `requirements.txt`) para que
  funcione igual en cualquier maquina, incluyendo Windows sin base de datos
  de zonas horarias propia.

### Probar el envio sin tocar Salesforce

`POST /slack/test-notify` manda el DM + el post del canal con datos de
mentira (no lee ni escribe nada en Salesforce, no valida titulo):

```bash
curl -X POST http://localhost:8000/slack/test-notify \
  -H "Content-Type: application/json" \
  -d '{"owner_email":"jackson.devia@colombiaesl.com","owner_name":"Jackson Devia"}'
```

o desde el navegador en `http://localhost:8000/docs` → `POST
/slack/test-notify` → **Try it out**.

## Como funciona el polling

`services/poller.py`:

1. Cada `POLL_INTERVAL_SECONDS` (default 60s), consulta Salesforce:
   `RecordType = B2B Company AND Owner = <usuario de integracion> AND
   Status NOT IN (...) AND CreatedDate > <checkpoint>`, hasta
   `POLL_BATCH_SIZE` Leads (default 20), ordenados por `CreatedDate` ascendente.
2. Si encuentra alguno, lo procesa con la misma logica que usaria el
   webhook (`handle_lead_ids` en `services/lead_assignment.py`): revalida el
   segmento, resuelve el equipo/owner por pais (ver "Reparto por pais"), y
   asigna.
3. Guarda en `poll_state.json` la fecha del ultimo Lead procesado, para que
   el proximo ciclo no vuelva a mirar los mismos.

### Leads con error: el checkpoint no se traba

- El checkpoint se detiene justo antes del primer Lead que dio error, para
  reintentarlo en el siguiente ciclo (si lo pasara, ese Lead no se volveria a
  consultar nunca).
- Si ese Lead sigue fallando `POLL_MAX_LEAD_RETRIES` ciclos seguidos (default
  5, ej. su equipo no tiene ningun usuario activo), el checkpoint lo pasa y el
  Lead queda en `retry_lead_ids` dentro de `poll_state.json`. Desde ahi se
  reintenta en cada ciclo, sin frenar a los Leads nuevos, y sale de la lista
  en cuanto deja de fallar (se asigna, o alguien lo asigno a mano). Se loguea
  como `ERROR` al pasarlo a la lista.
- Los Leads que el proceso ya manejo sin error (por ejemplo, un `skipped` que
  quedo detras de un error en el mismo lote) tampoco frenan el checkpoint.
  Antes podian dejar el poller trabado sin avisar.

### Revision hacia atras (leads que el checkpoint dejo atras)

El checkpoint avanza por `CreatedDate`, asi que un Lead puede quedar atras
sin que el bot lo haya visto nunca:

- entro al segmento despues de creado (ej. el caso de `Store Integration`:
  su owner se agrego tarde a `unassigned_owners`);
- Salesforce lo confirmo despues de otro Lead mas nuevo que ya se habia
  procesado, o dos Leads tienen el mismo `CreatedDate` y quedaron en ciclos
  distintos;
- se borro o se daño `poll_state.json` (el checkpoint vuelve a "ahora").

Por eso, en cada ciclo el bot tambien busca Leads del segmento creados en las
ultimas `POLL_LOOKBACK_HOURS` horas (default 72) con `CreatedDate` anterior o
igual al checkpoint que **siguen sin asignar**, y los procesa junto con los
nuevos (como maximo `POLL_BATCH_SIZE` por ciclo). Los que ya se asignaron no
aparecen porque cambiaron de owner. Los que este proceso ya evaluo (por
ejemplo, un `skipped` por filtro de calidad) no se vuelven a evaluar hasta el
proximo reinicio. Cada recuperacion se loguea como `WARNING` ("recuperados por
la revision"), asi se puede medir cuanto pasa. Si uno de estos da error, va a
`retry_lead_ids`. `POLL_LOOKBACK_HOURS=0` lo desactiva.

Ojo al activarlo por primera vez: asigna cualquier Lead del segmento de las
ultimas 72 h que siga sin owner real.

El intervalo, el tamaño de lote, los reintentos y la revision hacia atras son
configurables en `.env` (`POLL_INTERVAL_SECONDS`, `POLL_BATCH_SIZE`,
`POLL_MAX_LEAD_RETRIES`, `POLL_LOOKBACK_HOURS`) sin tocar codigo.

### Probar sin esperar el intervalo

```bash
curl -X POST http://localhost:8000/poll/run-once
```

Dispara un ciclo de inmediato y devuelve el resultado de cada Lead procesado
(o `"processed": []` si no habia nada nuevo desde el ultimo checkpoint).

### Procesar el backlog existente

Si en algun momento quieren que el bot tambien reasigne los ~5646 Leads que
ya estaban sin asignar antes de que el bot arrancara: borren
`poll_state.json` (o editen `last_created_date` a una fecha vieja, ej.
`"2000-01-01T00:00:00.000+0000"`) y reinicien el bot / llamen
`/poll/run-once` varias veces (`POLL_BATCH_SIZE` limita cuantos se procesan
por ciclo). Con el reparto por pais ya activo, cada lead se asignaria a su
equipo correspondiente (Brasil o Latam) segun su
`Country_of_Residence__c` — no a una sola persona.

## Correr localmente

```bash
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Se usa `python -m uvicorn` (no `uvicorn` solo) porque en Windows el ejecutable
de `uvicorn` no siempre queda en el `PATH`, aunque el paquete si este
instalado correctamente — con `python -m` se evita ese problema.

`GET /health` muestra si el polling esta activo, el intervalo, y el
checkpoint actual.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Modo Flow (para mas adelante)

El bot ya trae armado `POST /salesforce/lead-created`, que acepta tanto un
JSON de un Flow con accion "HTTP Callout" como el XML de un Outbound
Message clasico (Workflow Rule). Cuando consigan permisos de admin para
crear Named/External Credentials en el org:

1. Pongan `POLL_ENABLED=false` en `.env` (para no procesar cada lead dos
   veces, por polling y por webhook).
2. Expongan el puerto con `ngrok` (hay un `ngrok.exe` de ejemplo en
   `BOT_ASSIGNMENTS/BOT_SLACK_SALESFORCE/ngrok.exe`).
3. Creen un Record-Triggered Flow sobre `Lead`, disparado en `Create`, con
   Entry Conditions:
   - `Record Type` **equals** `B2B Company` (Id `01231000000xzVuAAI`)
   - `Owner ID` **equals** `0050Z000008e7RPQAY`
   - `Status` **not equal to** `Dead lead - Closed` Y **not equal to**
     `Dead Lead`
4. Accion "HTTP Callout" (via Named Credential) → `POST
   https://TU-DOMINIO/salesforce/lead-created`, body
   `{"lead_id": "{!$Record.Id}"}`, header `X-Webhook-Token:
   <LEAD_WEBHOOK_TOKEN del .env>`.

Alternativa sin Apex/Named Credential: un Workflow Rule con el mismo
criterio y una accion "Outbound Message" apuntando a
`https://TU-DOMINIO/salesforce/lead-created?token=<LEAD_WEBHOOK_TOKEN>` (el
bot detecta el XML SOAP y responde el Ack que Salesforce espera).

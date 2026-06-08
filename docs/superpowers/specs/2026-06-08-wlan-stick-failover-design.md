# WLAN-Stick als bevorzugte Verbindung mit Auto-Failover

**Datum:** 2026-06-08
**Status:** Design freigegeben, Implementierungsplan ausstehend
**Repo:** pingwatch (`~/Desktop/ping-monitor-project/pingwatch`, `github.com/sysbri/pingwatch`)

## Ziel

Der Raspberry Pi soll einen eingesteckten USB-WLAN-Stick automatisch als
**aktive Verbindung** nutzen (besseres Antennen-/Empfangsverhalten), und beim
Abziehen automatisch auf das interne WLAN (`wlan0`) zurückfallen. Es ist immer
**genau eine** Antenne aktiv. PingWatch überwacht jeweils die aktive Verbindung
und zeigt zusätzlich zur RSSI auch die **ausgehandelte Link-Rate live** an.

## Entscheidungen (aus dem Brainstorming)

- **Eine aktive Antenne mit Failover**, nicht Parallel-Vergleich beider Antennen.
  (Der Parallel-Vergleich wurde erwogen und bewusst verworfen — kein DB-Umbau,
  kein Zwei-Monitor, kein zweispuriges Dashboard.)
- **Stick wird die echte Verbindung** (trägt die Default-Route), nicht nur ein
  Mess-Interface. Stick raus → onboard übernimmt.
- **Datenrate = ausgehandelte Link-Rate** (PHY-Rate, z.B. „144 MBit/s"), live.
  Tatsächlicher Durchsatz und aktiver Speedtest sind **nicht** Teil dieses Specs
  (das bestehende `stream.*`/`speedtest.*`-Subsystem bleibt unberührt).
- **Freies WLAN ist offen mit Captive Portal.** Daraus folgt: Auto-Connect ohne
  Passwort; und der Stick braucht als neuer Client (eigene MAC) ggf. eine erneute
  Portal-Akzeptanz fürs Internet. Diese Konsequenz ist akzeptiert.

## Ausgangslage im Code (verifiziert)

- **Container** läuft `network_mode: host` und sieht alle Host-Interfaces direkt;
  `/sys/class/net` ist read-only nach `/host/sys/class/net` gemountet
  (`docker/docker-compose.yml`).
- **Monitor** (`src/pingwatch/wifi/monitor.py`): liest die Assoziation via
  nl80211 (primär) bzw. `iw` (Fallback). `run_wifi_monitor` startet
  `WifiMonitor(conn, bus)` **ohne Config** → Interface ist hart `wlan0`
  (`WifiConfig`-Default). Die Settings-`wifi.interface` wird zwar gelesen, aber
  **nie an den Monitor durchgereicht**.
- `link_rate_kbps` wird im **iw-Pfad** geparst (`rx bitrate`), im
  **nl80211-Pfad** auf `None` gesetzt → Link-Rate oft leer, obwohl nl80211 der
  bevorzugte Pfad ist.
- **Host-Helper** (`deploy/pingwatch-host-helper.sh`): `wifi_status` ruft hart
  `iw dev wlan0 link`; `wifi_scan`/`wifi_connect`/`wifi_forget` nutzen
  `nmcli dev wifi …` **ohne** `ifname` (NM wählt das Default-Gerät).
- `wifi.expected_ssid` existiert in den Settings, wird aber **nirgends benutzt**.
- **DB:** ein `schema.sql` (via `executescript`, alle `CREATE TABLE IF NOT
  EXISTS`) + `schema_version`-Tabelle (Wert 1). **Kein** Migrations-Framework.
  `wifi_rssi_samples`/`wifi_events` kennen **keine** Interface-Dimension
  (Single-Source); `wifi_rssi_samples.ts_ms` ist `PRIMARY KEY ... WITHOUT ROWID`
  (genau ein Sample pro Millisekunde — passt zu 1 Hz Single-Source).
- **`wifi_events.event_type` hat eine CHECK-Constraint** auf
  `('disconnect','connect','reassoc','channel_change','ssid_change','roam')`,
  und `WifiEventType` ist ein StrEnum mit denselben Werten. Ein **neuer** Event-Typ
  in dieser Tabelle würde abgewiesen bzw. bräuchte ein Table-Recreate — deshalb
  läuft der Antennenwechsel-Marker über eine **eigene additive Tabelle** (s.u.).
- `wifi_rssi_samples.link_rate_kbps` existiert bereits und wird gespeichert; der
  `/api/wifi/overview`-Endpoint liefert aber aktuell **nur** die RSSI-Serie.
- udev: nur `99-pingwatch-usb.rules` für USB-**Massenspeicher** (Mount). Keine
  WLAN-Interface-Regel. `pingwatch-source-watcher` ist nur Kiosk-Code-Reload.

## Architektur / Komponenten

### 1. Interface-Resolver — `src/pingwatch/netif.py` (reine stdlib)

- Liest ein konfigurierbares Sysfs-Wurzelverzeichnis (Default `/sys/class/net`;
  im Container `/host/sys/class/net`).
- WLAN-Interface = `…/<if>/wireless` (bzw. `…/<if>/phy80211`) existiert.
- USB-Klassifizierung: `realpath(…/<if>/device)` enthält `/usb`
  (onboard sitzt auf `mmc`/`platform`, nicht `usb`).
- Liefert das **eine aktive Interface**: ein USB-WLAN wenn vorhanden, sonst
  `wlan0` (bzw. den konfigurierten Fallback), sonst das erste WLAN-Interface.
  Zusätzlich ein Label (`usb` / `intern`) und den Interface-Namen für die Anzeige.
- `__main__`, das den Namen ausgibt, damit der Host-Helper (bash) denselben
  Resolver nutzen kann (`PYTHONPATH=/opt/pingwatch/src python3 -m pingwatch.netif
  --sysfs /sys/class/net`). Single Source of Truth.
- Reiner Logik-Anteil → über ein Sysfs-Fixture-Verzeichnis unit-testbar.

### 2. Route-Failover über NetworkManager (Kern des Features)

- **Beim Einstecken** (udev `add` auf USB-WLAN): neuer Host-Helper-Befehl
  `wifi_prefer_stick <ifname>`:
  - `nmcli dev wifi connect "<ssid>" ifname <stick>` (offen, kein Passwort).
    `<ssid>` = `wifi.expected_ssid`; wenn leer, abgeleitet aus der aktuell auf
    onboard verbundenen SSID.
  - Stick-Profil: `ipv4.route-metric 50` (onboard bleibt 100 → niedrigere Metric
    gewinnt die Default-Route), `connection.autoconnect yes`,
    `connection.autoconnect-retries 0` (unendlich, damit eine portal-limitierte
    Verbindung nicht abgebaut wird).
- **Beim Abziehen**: NM entfernt die Stick-Route automatisch → onboard (Metric
  100) übernimmt die Default-Route. Kein Polling, kein eigener Failover-Code.
- Idempotent: erneutes Einstecken desselben Sticks reconnectet über das
  persistente NM-Profil (USB-Sticks bekommen i.d.R. stabile `wlx<MAC>`-Namen).
- udev `remove` ist optional (Profil-Cleanup); fürs Failover nicht nötig.

### 3. Monitor folgt dem aktiven Interface — Single-Source

- `run_wifi_monitor` / `WifiMonitor` fragen pro 1-Hz-Tick den Resolver nach dem
  aktiven Interface und sampeln dieses. Hotplug ergibt sich automatisch.
- **Interface-Wechsel-Marker:** Wechselt das aktive Interface (Stick ↔ onboard),
  wird ein Eintrag in eine **neue additive Tabelle**
  `wifi_source_switches(ts_ms, from_if, to_if)` geschrieben (ergänzt als
  `CREATE TABLE IF NOT EXISTS` in `schema.sql` → wird auf Bestands-DBs beim
  nächsten Öffnen automatisch angelegt, **keine** Datenmigration, **kein**
  `schema_version`-Bump). Damit ist ein RSSI-/Link-Rate-Sprung im Chart als
  **Antennenwechsel** erkennbar und wird nicht als echter Signaleinbruch
  fehlinterpretiert. Die `wifi_events`-CHECK-Constraint bleibt unangetastet.
- `link_rate_kbps` wird auch auf dem **nl80211-Pfad** befüllt (Station-Info /
  Bitrate), sonst Fallback-Lesung der Bitrate via `iw dev <if> link`.

### 4. Link-Speed live im Dashboard — umschaltbar im selben Fenster

- Der `/api/wifi/overview`-Endpoint liefert **beide** Serien (RSSI **und**
  `link_rate_kbps`) sowie beide Live-Werte. Daten kommen aus
  `wifi_rssi_samples` (link_rate ist dort bereits gespeichert).
- Im Dashboard wird **dasselbe** Chart und **dieselbe** Kachel verwendet wie für
  RSSI, mit einem **Umschalter „RSSI ⇄ Link-Speed"**. Es wird immer nur die
  gewählte Metrik gezeigt — kein zweites Chart, keine zweite Kachel. Achsen-
  Beschriftung/Einheit (dBm bzw. MBit/s) wechselt mit. Eine Spur = das aktive
  Interface.

### 5. Host-Helper + Dashboard auf aktives Interface

- `wifi_status`/`scan`/`connect` zielen aufs vom Resolver gelieferte aktive
  Interface statt hart `wlan0` (`iw dev <if>`, `nmcli … ifname <if>`).
- Dashboard zeigt die **Quelle** an: „Stick (wlx…)" bzw. „Intern (wlan0)". Die
  RSSI-Kachel/-Chart bekommt den RSSI-⇄-Link-Speed-Umschalter aus Abschnitt 4
  (keine separate Link-Speed-Anzeige).

## Captive-Portal-Konsequenz (akzeptiert)

Weil der Stick die **Default-Route** trägt, läuft auch das Internet
(Ping/MTR/Heartbeat) über ihn. Der Stick hat eine eigene MAC → das Captive Portal
sieht einen neuen Client und verlangt ggf. erneute Akzeptanz, sonst kein Internet
über den Stick. **RSSI und Link-Rate funktionieren unabhängig davon** (kommen aus
der Assoziation). Bei jedem Antennenwechsel ist also ggf. einmal das Portal zu
bestätigen. Das ist der bewusst akzeptierte Preis dafür, den Stick wirklich als
Verbindung zu nutzen.

## Fehlerbehandlung

- Resolver fällt auf `wlan0` zurück, wenn Sysfs leer/unlesbar ist.
- Verschwindet das aktive Interface mitten im Lauf, lässt der nächste Tick es weg
  und nimmt das nächstbeste (bereits vorhandene „not connected"-Snapshot-Logik).
- Auto-Connect-Fehlschlag (z.B. Stick assoziiert nicht) wird geloggt; onboard und
  dessen Verbindung bleiben unberührt.

## Tests

- Unit: netif-Resolver gegen Sysfs-Fixture (USB-Bevorzugung, Fallback, leer).
- Unit: Monitor-Interface-Switch erzeugt genau einen `wifi_source_switches`-
  Eintrag (kein Doppel bei stabilem Interface); Event-Diff bleibt korrekt.
- Unit: `link_rate_kbps` wird auf beiden Sample-Pfaden befüllt.
- Unit: `/api/wifi/overview` liefert zusätzlich die Link-Rate-Serie und die
  Switch-Marker.
- Migration-/Idempotenz: Öffnen einer Bestands-DB legt `wifi_source_switches`
  an, ohne bestehende Daten/`schema_version` zu verändern.
- udev-/NM-/Host-Helper-Bash bleibt dünn und wird **auf dem Pi** verifiziert —
  insbesondere, dass das Route-Metric-Failover mit dem Captive Portal sauber
  spielt (Stick gewinnt Route bei Anwesenheit, onboard übernimmt beim Abziehen).

## Nicht im Scope

- Paralleles Messen beider Antennen / Antennen-Vergleich.
- DB-Interface-Dimension pro Sample.
- Tatsächlicher Durchsatz (Bytes/s) und aktiver Speedtest.
- Automatisches Durchklicken des Captive Portals.

## Auf dem Pi zu verifizieren

- Wie der konkrete Stick enumeriert (`iw dev`, `wlx<MAC>` vs. `wlanN`).
- NM-Verhalten mit zwei WLAN-Geräten am selben offenen Portal-Netz
  (Metric-Failover, dass die portal-limitierte Verbindung stabil bleibt).
- Ob der nl80211-Pfad die Bitrate liefert oder der iw-Fallback nötig ist.

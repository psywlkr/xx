Projektbericht (Struktur, Funktionen, Logik & Integrationen)
=============================================================

Dieser Bericht fasst den aktuellen Stand des Projekts zusammen und beantwortet
die Fragen zu Struktur, bereits enthaltenen Funktionen, Logik,
Telemetrie-Charakteristik sowie Dashboard-/Verknüpfungsaspekten.

Projektzweck
------------

Bleak ist eine plattformübergreifende, asynchrone Python-Bibliothek für
Bluetooth Low Energy (BLE). Das Projekt stellt einen GATT-Client bereit, der
BLE-Geräte finden, verbinden und über Characteristics/Descriptors lesen,
schreiben sowie Benachrichtigungen verarbeiten kann.

Architektur und Struktur
------------------------

Die Architektur ist in eine öffentliche API und mehrere Backend-Implementierungen
aufgeteilt:

* ``bleak/__init__.py``: öffentliche API mit ``BleakScanner`` und ``BleakClient``.
* ``bleak/backends/bluezdbus``: Linux/BlueZ-Backend.
* ``bleak/backends/corebluetooth``: macOS-CoreBluetooth-Backend.
* ``bleak/backends/winrt``: Windows-WinRT-Backend.
* ``bleak/backends/p4android``: Android-Backend (python-for-android).
* ``bleak/backends/{service,characteristic,descriptor,device}.py``:
  gemeinsame GATT-Datenmodelle.
* ``bleak/args``: backend-spezifische Konfigurationsargumente.
* ``tests``: Unit- und Integrations-Tests.
* ``examples``: Beispielskripte für Discovery, Notify, Read/Write, UART usw.
* ``docs``: Sphinx-Dokumentation.

Bereits enthaltene Funktionalität
---------------------------------

**Scanning / Discovery**

* Geräte finden (inkl. gefilterter Suche nach Name, Adresse oder Prädikat).
* Zugriff auf Advertising-Daten (z. B. UUIDs, Herstellerdaten, RSSI).

**Verbindung und Kommunikation**

* Verbindungen aufbauen/trennen.
* Pairing/Unpairing (plattformabhängig).
* GATT Characteristics lesen und schreiben (mit/ohne Response).
* Notifications/Indications abonnieren und beenden.
* GATT Descriptors lesen und schreiben.
* Services/Characteristics/Descriptors als strukturierte Sammlung verfügbar.

**Asynchrone Logik**

* Durchgängiges ``asyncio``-Modell.
* Kontextmanager-Unterstützung für Scanner und Client.

Logik-Charakteristik
--------------------

* Einheitliche High-Level-API, intern dynamische Auswahl des passenden Backends
  je Betriebssystem.
* Strikte Trennung zwischen API-Oberfläche und plattformspezifischen Details.
* Fehlerbehandlung über dedizierte Exceptions (z. B. BLE/GATT-spezifische Fehler).

Telemetrie, Monitoring und Dashboard
------------------------------------

**Aktuell enthalten**

* Standard-Logging über Python ``logging``.
* Aktivierbares Debug-Logging über Umgebungsvariable ``BLEAK_LOGGING``.

**Aktuell nicht enthalten**

* Keine produktseitige Telemetrie-/Analytics-Pipeline.
* Kein eingebautes Metrik-Exporting (z. B. Prometheus/OpenTelemetry).
* Kein internes Dashboard im Projektcode.

Verknüpfungen / Integrationen
-----------------------------

* CI/CD über GitHub Actions (Build, Tests, Linting, Typprüfung).
* Dokumentations-Build mit Sphinx.
* Test- und Coverage-Integration über Pytest/Codecov in der CI.
* Plattformabhängige BLE-Integrationen über BlueZ, CoreBluetooth und WinRT.

Qualitätssicherung im aktuellen Stand
-------------------------------------

Die vorhandene Projektkonfiguration enthält:

* Tests mit ``pytest`` (inkl. asynchroner Tests und Coverage).
* Linting/Formatierung mit ``flake8``, ``isort`` und ``black``.
* Typprüfung mit ``pyright`` und ``mypy``.
* Dokumentations-Build mit ``sphinx-build``.

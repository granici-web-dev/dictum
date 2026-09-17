ABC-123 Login-Formular vor dem Absenden prüfen
https://jira.example.com/browse/ABC-123

Typ: Story
Priorität: Mittel
Autor: Teamlead Frontend

Beschreibung
Die Felder des Login-Formulars sollen im Browser geprüft werden, bevor eine Anfrage an den Server geht. Die API bleibt unverändert, sie gehört dem Backend-Team.

Akzeptanzkriterien
- Ein leeres Pflichtfeld zeigt eine Fehlermeldung unter dem Feld.
- Eine ungültige E-Mail-Adresse zeigt eine Fehlermeldung unter dem Feld.
- Solange ein Feld ungültig ist, geht keine Anfrage an die API.

Fällig: 26.09.2026

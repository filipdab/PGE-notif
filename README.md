# PGE-notif

Powiadomienia e-mail (opcjonalnie SMS) o **planowanych wyłączeniach prądu** i **awariach**
PGE Dystrybucja dla wybranych ulic — bo oficjalnego newslettera nie zawsze da się zamówić.

- sprawdza dane co 30 minut (timer systemd),
- o planowanym wyłączeniu informuje **dzień wcześniej** (z ciszą nocną 22:00–7:00),
- o awarii informuje od razu,
- każde wyłączenie zgłasza tylko raz,
- weryfikuje miejscowość — PGE często publikuje jedną awarię dla wielu miejscowości
  („Miasto A: ul. X; Miasto B: ul. Y”), a ta sama nazwa ulicy występuje w wielu miejscach.

Czysty Python 3.9+, bez zewnętrznych bibliotek.

## Źródło danych

Nieoficjalne — publiczne API, z którego korzysta strona
[pgedystrybucja.pl/wylaczenia](https://pgedystrybucja.pl/wylaczenia/planowane-wylaczenia)
(`falcon.gkpge.pl`, `power-outage.gkpge.pl`). PGE może je zmienić bez zapowiedzi.

## Instalacja (Raspberry Pi / Debian)

```bash
git clone https://github.com/filipdab/PGE-notif.git ~/pge_wylaczenia
cd ~/pge_wylaczenia
cp env.example .env && chmod 600 .env && nano .env     # adresy + dane poczty
python3 pge_wylaczenia.py --test-mail                   # czy poczta działa
python3 pge_wylaczenia.py --list                        # czy trafia w dobre ulice (linie TERYT:)
```

Uruchamianie co 30 minut:

```bash
sed -i "s/UZYTKOWNIK/$USER/g" pge-wylaczenia.service
sudo cp pge-wylaczenia.service pge-wylaczenia.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pge-wylaczenia.timer
```

## Konfiguracja (`.env`)

| Zmienna | Opis |
|---|---|
| `PGE_ADRESY` | **wymagane**: `Miasto: Ulica1, Ulica2; Inna Miejscowość: Ulica3` |
| `PGE_POWIAT` | gdy w Polsce jest kilka miejscowości o tej nazwie |
| `EMAIL_FROM`, `EMAIL_PASSWORD` | konto nadawcy (Gmail: [hasło aplikacji](https://myaccount.google.com/apppasswords)) |
| `SMTP_SERVER`, `SMTP_PORT` | domyślnie `smtp.gmail.com`, `465` |
| `PGE_MAIL_TO` | adresaci po przecinku (domyślnie `EMAIL_FROM`) |
| `SMSAPI_TOKEN`, `SMS_TO` | SMS przez SMSAPI.pl |

Rejon energetyczny obejmuje zwykle okoliczne gminy — wpisz miejscowość, w której ulica
faktycznie leży, a nie najbliższe miasto.

## Opcje

```
--list                 nadchodzące wyłączenia dla Twoich ulic (nic nie wysyła)
--dry-run              co zostałoby wysłane
--test-mail            testowy e-mail
--test-powiadomienie   przykładowe powiadomienie (jutro 8–14)
--zrzut                zapis surowych danych PGE do ~/pge_zrzut_*.json (diagnostyka)
```

Odrzucone wyłączenia z innych miejscowości: `journalctl -u pge-wylaczenia -g Pominięto`.

## Autor

Filip Dąbrowski — filip@dabrowski.in

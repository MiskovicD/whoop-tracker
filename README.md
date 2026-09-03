# Whoop-tracker

Je Whoop 4.0 uitlezen zonder abonnement. Alles draait op je eigen Mac; er gaat
niets naar Whoop.

Je band blijft namelijk gewoon meten als je abonnement afloopt — hij bewaart
per seconde je hartslag, beweging, huidtemperatuur en de intervallen tussen je
hartslagen. Alleen de officiële app weigert die op te halen. Deze gereedschappen
doen dat wel.

## Wat je krijgt

| Werkt | Werkt niet |
|---|---|
| Hartslag, rusthartslag, HRV (RMSSD/SDNN) | Zuurstofsaturatie |
| Slaap: duur, efficiëntie, ontwaken | Slaapfasen (REM/diep) |
| Belasting: Edwards en Banister TRIMP, zones | Whoop's eigen scores 1-op-1 |
| Ademhaling, huidtemperatuur, stress | |
| Herstelscore (na 7 nachten) | |
| Wekker uitlezen en zetten | |

Zuurstof is geen kwestie van beter programmeren: dat vereist de hartslaggolf op
25 Hz of hoger, terwijl de historie één meting per seconde bevat. Die informatie
bestaat niet in de data.

## Wat je nodig hebt

- Een **Whoop 4.0** (5.0 werkt niet met deze scripts)
- Een Mac met Bluetooth
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Geen Whoop-abonnement, geen account

## Opzetten

**1. De protocol-client van OpenStrap** (niet van ons, MIT-licentie):

```bash
git clone https://github.com/OpenStrap/research.git ~/Desktop/whoop-research
```

Alle scripts hier verwachten hem op precies dat pad.

**2. Deze repo:**

```bash
git clone https://github.com/MiskovicD/E-portfolio.git
cd E-portfolio/whoop-tracker
```

**3. Zet je band aan de lader** tot hij weer knippert. Ligt hij al maanden stil,
dan is de accu leeg en werkt niets.

**4. Zoek je band:**

```bash
cd ~/Desktop/whoop-research
uv run --no-project --with bleak python research_playground.py scan
```

Je zoekt een regel als `found WHOOP <naam> @ <UUID>`. Dat UUID heb je zo nodig.

> **Ziet hij niets en zegt hij "Bluetooth device is turned off" terwijl Bluetooth
> aan staat?** Dan is het de macOS-privacyinstelling, niet je Bluetooth. Zet
> Terminal aan onder Systeeminstellingen → Privacy en beveiliging → Bluetooth.

**5. Controleer de verbinding:**

```bash
uv run --no-project --with bleak python research_playground.py --address <UUID> info
```

Krijg je `GET_HELLO_HARVARD` met een batterijpercentage terug, dan staat alles open.

## Dagelijks gebruik

Eén commando doet alles: back-up, leegtrekken, doorrekenen, en versturen naar de app.

```bash
uv run --no-project --with bleak python whoop_update.py --age 23 --drain --quick --save-daily
```

Vervang `--age` door je eigen leeftijd (dat bepaalt je geschatte maximale
hartslag). Ken je je gemeten maximum, gebruik dan `--hrmax 191` — dat is altijd
nauwkeuriger dan een formule.

Zonder Supabase erachter werkt alles behalve de laatste stap. Voor een rapport
in je browser:

```bash
python3 whoop_report.py ~/Desktop/whoop-research/whoop.db
open ~/Desktop/whoop-research/whoop_report.html
```

## Twee dingen die je moet weten

**Je band neemt alleen op terwijl je hem draagt.** De historie stopt op de
seconde dat je hem afdoet. Draag hem ook tijdens het laden — het batterijpakje
schuift er juist overheen zodat dat kan.

**Je band wist wat hij verstuurd heeft.** In zijn eigen log staat `Trim:` na
elke geslaagde overdracht. `whoop.db` is dus de enige kopie van je geschiedenis;
`whoop_update.py` maakt daarom eerst een back-up. Gebruik ook geen tweede app
(zoals NOOP) naast deze: wie het eerst leegtrekt, krijgt de data.

## Waarom het leegtrekken zoveel rondes kost

De band stuurt per sync één burst, meldt `Historical Dump Complete` en stopt.
Bij een achterstand van uren zijn dat dus veel rondes. `--drain` blijft draaien
tot hij de werkelijke tijd inhaalt. Trek je dagelijks leeg, dan is het één ronde
van tien seconden.

## De app op je telefoon

Open in **Safari** (niet Chrome — op iOS mag alleen Safari een PWA installeren):

```
https://miskovicd.github.io/E-portfolio/whoop-tracker/app/
```

Deelknop → *Zet op beginscherm*. Let op de hoofdletter E in de URL.

De app leest uit Supabase, dus je hebt een account nodig in hetzelfde project.
Vraag daarom. Wil je je gegevens liever gescheiden houden, maak dan een eigen
gratis Supabase-project aan, draai `supabase-schema.sql` en pas `SB_URL` en
`SB_ANON` aan in `app/index.html` en `whoop_push.py`.

## Bijwerken

De **app** werkt zichzelf bij: de service worker is netwerk-eerst, dus je krijgt
vanzelf de nieuwste versie.

De **scripts** niet. Die haal je zelf op:

```bash
git pull
```

Die twee lopen dus uit de pas. Verandert er iets aan het datamodel, dan kan je
app een veld verwachten dat je oude script nog niet stuurt. Trek na een
app-wijziging dus even `git pull`. Wil je daar geen last van hebben: fork de
repo, dan bepaal je zelf wanneer je wijzigingen overneemt.

## Verder gereedschap

| Script | Waarvoor |
|---|---|
| `whoop_drain.py` | alleen leegtrekken, tot de band bij is |
| `whoop_metrics.py` | doorrekenen; `--herbouw-baseline` bouwt je baseline opnieuw op |
| `whoop_alarm.py` | wekker uitlezen, zetten, testen, uitzetten |
| `whoop_hr.py` | GATT-services, betrouwbare accustand, standaard hartslagprofiel |
| `whoop_insights.py` | correlaties tussen je metingen; `--uitleg` laat Claude ze duiden |

`whoop_hr.py services` laat trouwens zien dat je band het **standaard
Bluetooth-hartslagprofiel** aanbiedt. Elke gewone hartslag-app op je telefoon
kan hem dus als hartslagband gebruiken — zolang je Mac niet verbonden is, want
de band bindt aan één apparaat tegelijk.

## Herstel en patronen hebben tijd nodig

Een herstelscore is een vergelijking met jouw normaal. Zonder dat normaal is elk
getal verzonnen, dus de scripts weigeren er een te tonen onder de **7 nachten**.
De patroonanalyse begint bij **8 nachten**. Dat is geen ontbrekende functie maar
statistiek.

Voeg elke ochtend je eigen oordeel toe met `--gevoel 1..5`. Dat is de enige maat
die weet wat "goed" voor jóu betekent, en zonder die maat blijft elk advies een
gok.

## Waar dit op gebouwd is

Het protocolwerk komt van [OpenStrap](https://github.com/OpenStrap) en
[bWanShiTong](https://github.com/bWanShiTong/reverse-engineering-whoop-post).
Zonder hun reverse engineering was hier niets van mogelijk.

Geen medisch apparaat. Alle waarden zijn benaderingen.

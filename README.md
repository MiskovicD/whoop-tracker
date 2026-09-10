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

- Een **Whoop 4.0** (5.0 spreekt een ander protocol en werkt niet)
- Een Mac met Bluetooth
- [`uv`](https://docs.astral.sh/uv/) — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Geen Whoop-abonnement en geen Whoop-account

## Opzetten

Twee commando's, daarna wijst het programma je de weg.

```bash
git clone https://github.com/MiskovicD/whoop-tracker.git ~/whoop-tracker
cd ~/whoop-tracker && ./installeer.sh
```

`installeer.sh` controleert je gereedschap, haalt de band-client van OpenStrap
op, bouwt **Whoop.app** in `~/Applications` en opent hem. Ontbreekt er iets,
dan zegt hij precies wat en stopt hij; hij installeert nooit iets buiten je
weten om.

> **Niet in `~/Desktop`, `~/Documents` of `~/Downloads` zetten.** macOS
> schermt die mappen af voor programma's zonder ondertekening. De app start
> dan, kan zijn eigen bestanden niet lezen en sluit meteen weer - van buiten
> niet te onderscheiden van "hij doet niets". Het installatiescript weigert
> daarom in die mappen.

### In het venster

Drie stappen, eenmalig:

1. **Je account** - maak er een aan, of log in. Hiermee komen je metingen in de
   telefoon-app, en alleen jij ziet je eigen gegevens.
2. **Je band** - doe hem van je pols en tik twee keer op het scherm, dan is hij
   vindbaar. *Zoek mijn band* laat zien wat er in de buurt is; je kiest de jouwe.
3. **Jij** - je leeftijd, of je gemeten maximale hartslag als je die kent. Dat
   bepaalt je belastingscore. Zonder dit getal weigeren de scores een waarde,
   want dan zijn ze verzonnen.

Daarna is **Leegtrekken** één klik. Zet in de instellingen de uursync aan, dan
hoef je er helemaal niet meer aan te denken: je Mac probeert het elk uur, en
mislukt stil als je band buiten bereik is.

## Zonder het venster

Alles kan ook vanaf de opdrachtregel. Je instellingen komen uit hetzelfde
bestand, dus je hoeft je leeftijd niet mee te geven.

```bash
uv run --no-project --with bleak python whoop_update.py --drain --quick --save-daily
```

Of alleen leegtrekken, doorrekenen, of versturen:

```bash
./whoop_auto.sh inhalen        # lange inhaalslag, met vergrendeling en bewaker
python3 whoop_report.py        # rapport in je browser
python3 whoop_config.py        # laat je instellingen zien
```

Je instellingen staan in `~/.whoop-tracker/config.json`. Alles in die map is
jouw staat en blijft; alles buiten die map is code en mag weg.

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
https://miskovicd.github.io/whoop-tracker/app/
```

Deelknop → *Zet op beginscherm*.

Onderaan staat **Slaapdoel**: daar zet je op hoeveel slaap je mikt. De
slaapring vergelijkt met dat getal, en het staat per persoon los - hiervoor
was het voor iedereen acht uur.

De eerste keer krijg je een inlogscherm. Tik op **Account maken**, vul een
e-mailadres en wachtwoord in, en je bent binnen — er komt geen bevestigingsmail
aan te pas. Vergeten? *Wachtwoord vergeten?* stuurt je een herstel-link.

Daarna log je op je Mac één keer in met dezelfde gegevens, zodat je band zijn
metingen naar jóuw account stuurt. Dat vraagt `whoop_update.py` vanzelf de
eerste keer; alleen de refresh-token blijft achter in
`~/.whoop-tracker/session.json`, nooit je wachtwoord.

**Over gedeelde opslag:** iedereen zit in hetzelfde Supabase-project, maar
row level security staat aan — de database geeft je alleen rijen terug waar
`user_id` gelijk is aan jouw eigen id. Je ziet dus niemand anders, en niemand
anders ziet jou. Wil je het toch volledig op jezelf hebben, maak dan een eigen
gratis Supabase-project aan, draai `supabase-schema.sql` in de SQL Editor en pas
`SB_URL` en `SB_ANON` aan in `app/index.html` en `whoop_push.py`.

## Bijwerken

De **app** werkt zichzelf bij: de service worker is netwerk-eerst, dus je krijgt
vanzelf de nieuwste versie.

De **scripts** niet. Die haal je zelf op:

```bash
cd ~/whoop-tracker && git pull
```

Staat je map buiten `~/Desktop` en dergelijke, dan wijzen de app en de uursync
rechtstreeks naar je map en ben je meteen bij. Staat hij er wel in, dan draait
de uursync vanaf een kopie en moet je na een `git pull` opnieuw
`./whoop_auto.sh install` doen.

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
| `whoop_app.py` | het venster met de knop; `maak-app.sh` bouwt de app-bundel |
| `whoop_config.py` | je instellingen bekijken; zonder argumenten print hij ze |

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

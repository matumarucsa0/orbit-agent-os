# Static datasets

## Name days: `namedays_sk.json`, `namedays_cz.json`

Format: `{"MM-DD": ["Name", ...]}` for all 366 days (29 Feb included). Days without a name
(public holidays) map to `[]`. A name may appear on several days; the resolver returns all of them.
The files are regenerated with `scripts/build_namedays.py` (see below), not edited by hand.

### Slovakia (`namedays_sk.json`)
- **Source:** *Oficiálne kalendárium 2025*, prepared by the Calendar Commission at the Ministry of Culture of the
  Slovak Republic (Kalendárová komisia pri Ministerstve kultúry SR), which includes linguists of the Ľudovít Štúr
  Institute of Linguistics (JÚĽŠ SAV). This is the official Slovak name calendar.
  - Page: https://www.culture.gov.sk/sk/oficialne-kalendarium
  - File: https://www.culture.gov.sk/storage/2020/03/Oficialne-kalendarium_2025.pdf
    (accessed 2026-09-23, SHA-256 `7c4855e5411fb2998043534fb0381678d10c4d5f486bb4a6755091b07142ce57`)
- **Only the official names** from the PDF are used, in the given order. Extended lists add rarer variants and are
  not included.
- **No name:** 1 Jan, 1 May, 2 Nov, 25 Dec.
- **Cross-check:** against the bold (official) names on sk.wikipedia
  [*Meniny na Slovensku*](https://sk.wikipedia.org/wiki/Meniny_na_Slovensku). 67 days differ. Almost all of them are
  names added in the 2025 edition that the older Wikipedia list lacks (e.g. Sandra on 2 Jan, Mia on 12 Sep). The rest
  are 2025 changes: Drahomíra moved from 19 Jan to 16 Jul, Dobroslava from 11 Jun to 15 Jan, and the spellings Daša
  and Oskar. The official PDF wins.

### Czechia (`namedays_cz.json`)
- **There is no official Czech name calendar.** Until 1989 the calendar commission of Obchodní tiskárny Kolín
  maintained it. Since then publishers may choose names freely, and most of them use the civil calendar from the early
  1990s, sometimes adding extra names (see the introduction and references of the Wikipedia article below).
- **Source:** the traditional civil calendar as listed on cs.wikipedia
  [*Jmeniny v Česku*](https://cs.wikipedia.org/wiki/Jmeniny_v_%C4%8Cesku), raw wikitext accessed 2026-09-23.
  - For each day only the name(s) before the first comma are kept. These are the civil-calendar names, and pairs are
    written with " a ", e.g. "Adam a Eva" or "Cyril a Metoděj". Extra names after a comma are not included.
- **No name:** 1 Jan, 6 Jan, 1 May, 8 May, 6 Jul, 25 Dec.
- **Cross-check:** against [kalendar-online.cz](https://www.kalendar-online.cz/jmena-a-svatky/kalendar-jmen), which
  uses an extended list. 10 of 360 days differ, all known differences between Czech calendars:
  - spelling variants: 14 Feb Valentin/Valentýn, 4 Mar Stella/Stela, 14 Mar Rut/Rút, 7 Oct Justina/Justýna,
    23 Oct Theodor/Teodor, 28 Oct Jidáš/Juda
  - 22 Feb Petr (Stolec sv. Petra)
  - 7 Jul Bohuslava (kalendar-online: Hostivít)
  - 31 Oct Štěpánka (kalendar-online: none)

  The Wikipedia civil calendar is kept.

### Regenerating
Download the sources into a scratch directory (not the repo):

```bash
curl -L -o Oficialne-kalendarium_2025.pdf https://www.culture.gov.sk/storage/2020/03/Oficialne-kalendarium_2025.pdf
curl -L -o cz_wikipedia.wiki "https://cs.wikipedia.org/w/index.php?title=Jmeniny_v_%C4%8Cesku&action=raw"
curl -L -o sk_wikipedia.wiki "https://sk.wikipedia.org/w/index.php?title=Meniny_na_Slovensku&action=raw"
curl -L -o cz_kalendar_online.html https://www.kalendar-online.cz/jmena-a-svatky/kalendar-jmen
```

Then run the generator. `pypdf` is used only for this one-off run and is not a project dependency:

```bash
uvx --with pypdf python scripts/build_namedays.py --sk-pdf Oficialne-kalendarium_2025.pdf \
    --cz-wiki cz_wikipedia.wiki --check-sk-wiki sk_wikipedia.wiki --check-cz-html cz_kalendar_online.html
```

`tests/test_namedays_data.py` spot-checks well-known dates for both countries.

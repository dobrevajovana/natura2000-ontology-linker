"""
Step 1 — Scrape Natura 2000 Terms

Fetches habitat types, species, biogeographical regions, and conservation
status categories from EEA/Natura 2000 sources. Falls back to a hardcoded
reference list if web scraping fails.

Output: {data_dir}/terms.json
"""

import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("natura2000.scraper")

USER_AGENT = "Natura2000OntologyLinker/1.0 (research project)"
RATE_LIMIT = float(os.environ.get("API_RATE_LIMIT", "1.0"))
DRY_RUN_SIZE = int(os.environ.get("DRY_RUN_SIZE", "50"))

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# ---------------------------------------------------------------------------
# Internal reference data — used only when ALL live sources fail.
# Based on the official Habitats Directive Annex I (Council Directive 92/43/EEC).
# ---------------------------------------------------------------------------

_FALLBACK_HABITATS = [
    # 1xxx — Coastal and halophytic habitats
    {"code": "1110", "label": "1110 Sandbanks which are slightly covered by seawater all the time", "priority": False},
    {"code": "1120", "label": "1120 * Posidonia beds", "priority": True},
    {"code": "1130", "label": "1130 Estuaries", "priority": False},
    {"code": "1140", "label": "1140 Mudflats and sandflats not covered by seawater at low tide", "priority": False},
    {"code": "1150", "label": "1150 * Coastal lagoons", "priority": True},
    {"code": "1160", "label": "1160 Large shallow inlets and bays", "priority": False},
    {"code": "1170", "label": "1170 Reefs", "priority": False},
    {"code": "1180", "label": "1180 Submarine structures made by leaking gases", "priority": False},
    {"code": "1210", "label": "1210 Annual vegetation of drift lines", "priority": False},
    {"code": "1220", "label": "1220 Perennial vegetation of stony banks", "priority": False},
    {"code": "1230", "label": "1230 Vegetated sea cliffs of the Atlantic and Baltic coasts", "priority": False},
    {"code": "1240", "label": "1240 Vegetated sea cliffs of the Mediterranean coasts with endemic Limonium spp.", "priority": False},
    {"code": "1250", "label": "1250 Vegetated sea cliffs with endemic flora of the Macaronesian coasts", "priority": False},
    {"code": "1310", "label": "1310 Salicornia and other annuals colonizing mud and sand", "priority": False},
    {"code": "1320", "label": "1320 Spartina swards", "priority": False},
    {"code": "1330", "label": "1330 Atlantic salt meadows", "priority": False},
    {"code": "1340", "label": "1340 * Inland salt meadows", "priority": True},
    {"code": "1350", "label": "1350 * Halophilous scrubs", "priority": True},
    {"code": "1410", "label": "1410 Mediterranean salt meadows", "priority": False},
    {"code": "1420", "label": "1420 Mediterranean and thermo-Atlantic halophilous scrubs", "priority": False},
    {"code": "1430", "label": "1430 Halo-nitrophilous scrubs", "priority": False},
    {"code": "1510", "label": "1510 * Mediterranean salt steppes", "priority": True},
    {"code": "1520", "label": "1520 * Iberian gypsum vegetation", "priority": True},
    {"code": "1530", "label": "1530 * Pannonic salt steppes and salt marshes", "priority": True},
    # 2xxx — Coastal sand dunes and inland dunes
    {"code": "2110", "label": "2110 Embryonic shifting dunes", "priority": False},
    {"code": "2120", "label": "2120 Shifting dunes along the shoreline with Ammophila arenaria", "priority": False},
    {"code": "2130", "label": "2130 * Fixed coastal dunes with herbaceous vegetation (grey dunes)", "priority": True},
    {"code": "2140", "label": "2140 * Decalcified fixed dunes with Empetrum nigrum", "priority": True},
    {"code": "2150", "label": "2150 * Atlantic decalcified fixed dunes", "priority": True},
    {"code": "2160", "label": "2160 Dunes with Hippophae rhamnoides", "priority": False},
    {"code": "2170", "label": "2170 Dunes with Salix repens ssp. argentea", "priority": False},
    {"code": "2180", "label": "2180 Wooded dunes of the Atlantic, Continental and Boreal region", "priority": False},
    {"code": "2190", "label": "2190 Humid dune slacks", "priority": False},
    {"code": "2210", "label": "2210 Crucianellion maritimae fixed beach dunes", "priority": False},
    {"code": "2220", "label": "2220 Dunes with Euphorbia terracina", "priority": False},
    {"code": "2230", "label": "2230 Malcolmietalia dune grasslands", "priority": False},
    {"code": "2240", "label": "2240 Brachypodietalia dune grasslands with annuals", "priority": False},
    {"code": "2250", "label": "2250 * Coastal dunes with Juniperus spp.", "priority": True},
    {"code": "2260", "label": "2260 Cisto-Lavenduletalia dune sclerophyllous scrubs", "priority": False},
    {"code": "2270", "label": "2270 * Wooded dunes with Pinus pinea and/or Pinus pinaster", "priority": True},
    {"code": "2310", "label": "2310 Dry sand heaths with Calluna and Genista", "priority": False},
    {"code": "2320", "label": "2320 Dry sand heaths with Calluna and Empetrum nigrum", "priority": False},
    {"code": "2330", "label": "2330 Inland dunes with open Corynephorus and Agrostis grasslands", "priority": False},
    {"code": "2340", "label": "2340 * Pannonic inland dunes", "priority": True},
    # 3xxx — Freshwater habitats
    {"code": "3110", "label": "3110 Oligotrophic waters containing very few minerals of sandy plains", "priority": False},
    {"code": "3120", "label": "3120 Oligotrophic waters containing very few minerals generally on sandy soils of the West Mediterranean", "priority": False},
    {"code": "3130", "label": "3130 Oligotrophic to mesotrophic standing waters with vegetation of the Littorelletea uniflorae and/or of the Isoeto-Nanojuncetea", "priority": False},
    {"code": "3140", "label": "3140 Hard oligo-mesotrophic waters with benthic vegetation of Chara spp.", "priority": False},
    {"code": "3150", "label": "3150 Natural eutrophic lakes with Magnopotamion or Hydrocharition type vegetation", "priority": False},
    {"code": "3160", "label": "3160 Natural dystrophic lakes and ponds", "priority": False},
    {"code": "3170", "label": "3170 * Mediterranean temporary ponds", "priority": True},
    {"code": "3180", "label": "3180 * Turloughs", "priority": True},
    {"code": "3190", "label": "3190 Lakes with gypsum", "priority": False},
    {"code": "3210", "label": "3210 Fennoscandian natural rivers", "priority": False},
    {"code": "3220", "label": "3220 Alpine rivers and the herbaceous vegetation along their banks", "priority": False},
    {"code": "3230", "label": "3230 Alpine rivers and their ligneous vegetation with Myricaria germanica", "priority": False},
    {"code": "3240", "label": "3240 Alpine rivers and their ligneous vegetation with Salix elaeagnos", "priority": False},
    {"code": "3250", "label": "3250 Constantly flowing Mediterranean rivers with Glaucium flavum", "priority": False},
    {"code": "3260", "label": "3260 Water courses of plain to montane levels with the Ranunculion fluitantis and Callitricho-Batrachion vegetation", "priority": False},
    {"code": "3270", "label": "3270 Rivers with muddy banks with Chenopodion rubri p.p. and Bidention p.p. vegetation", "priority": False},
    {"code": "3280", "label": "3280 Constantly flowing Mediterranean rivers with Paspalo-Agrostidion species and hanging curtains of Salix and Populus alba", "priority": False},
    {"code": "3290", "label": "3290 Intermittently flowing Mediterranean rivers of the Paspalo-Agrostidion", "priority": False},
    # 4xxx — Temperate heath and scrub
    {"code": "4010", "label": "4010 Northern Atlantic wet heaths with Erica tetralix", "priority": False},
    {"code": "4020", "label": "4020 * Temperate Atlantic wet heaths with Erica ciliaris and Erica tetralix", "priority": True},
    {"code": "4030", "label": "4030 European dry heaths", "priority": False},
    {"code": "4040", "label": "4040 * Dry Atlantic coastal heaths with Erica vagans", "priority": True},
    {"code": "4050", "label": "4050 * Endemic macaronesian heath", "priority": True},
    {"code": "4060", "label": "4060 Alpine and Boreal heaths", "priority": False},
    {"code": "4070", "label": "4070 * Bushes with Pinus mugo and Rhododendron hirsutum", "priority": True},
    {"code": "4080", "label": "4080 Sub-Arctic Salix spp. scrub", "priority": False},
    {"code": "4090", "label": "4090 Endemic oro-Mediterranean heath with gorse", "priority": False},
    {"code": "40A0", "label": "40A0 * Subcontinental peri-Pannonic scrub", "priority": True},
    {"code": "40B0", "label": "40B0 Rhodopic and Moesian oak forests", "priority": False},
    {"code": "40C0", "label": "40C0 * Ponto-Sarmatic deciduous thickets", "priority": True},
    # 5xxx — Sclerophyllous scrub (matorral)
    {"code": "5110", "label": "5110 Stable xero-thermophilous formations with Buxus sempervirens", "priority": False},
    {"code": "5120", "label": "5120 Mountain Cytisus purgans formations", "priority": False},
    {"code": "5130", "label": "5130 Juniperus communis formations on heaths or calcareous grasslands", "priority": False},
    {"code": "5140", "label": "5140 * Cistus palhinhae formations on maritime wet heaths", "priority": True},
    {"code": "5210", "label": "5210 Arborescent matorral with Juniperus spp.", "priority": False},
    {"code": "5220", "label": "5220 * Arborescent matorral with Ziziphus", "priority": True},
    {"code": "5230", "label": "5230 * Arborescent matorral with Laurus nobilis", "priority": True},
    {"code": "5310", "label": "5310 Laurus nobilis thickets", "priority": False},
    {"code": "5320", "label": "5320 Low formations of Euphorbia close to cliffs", "priority": False},
    {"code": "5330", "label": "5330 Thermo-Mediterranean and pre-desert scrub", "priority": False},
    {"code": "5410", "label": "5410 West Mediterranean clifftop phryganas", "priority": False},
    {"code": "5420", "label": "5420 Sarcopoterium spinosum phryganas", "priority": False},
    {"code": "5430", "label": "5430 Endemic phryganas of the Euphorbio-Verbascion", "priority": False},
    # 6xxx — Natural and semi-natural grassland formations
    {"code": "6110", "label": "6110 * Rupicolous calcareous or basophilic grasslands of the Alysso-Sedion albi", "priority": True},
    {"code": "6120", "label": "6120 * Xeric sand calcareous grasslands", "priority": True},
    {"code": "6130", "label": "6130 Calaminarian grasslands of the Violetalia calaminariae", "priority": False},
    {"code": "6140", "label": "6140 Siliceous Pyreneo-Cantabrian grasslands with Festuca eskia", "priority": False},
    {"code": "6150", "label": "6150 Siliceous alpine and boreal grasslands", "priority": False},
    {"code": "6160", "label": "6160 Oro-Iberian Festuca indigesta grasslands", "priority": False},
    {"code": "6170", "label": "6170 Alpine and subalpine calcareous grasslands", "priority": False},
    {"code": "6180", "label": "6180 Macaronesian mesophile grasslands", "priority": False},
    {"code": "6190", "label": "6190 Rupicolous Pannonic grasslands", "priority": False},
    {"code": "6210", "label": "6210 Semi-natural dry grasslands and scrubland facies on calcareous substrates", "priority": False},
    {"code": "6220", "label": "6220 * Pseudo-steppe with grasses and annuals of the Thero-Brachypodietea", "priority": True},
    {"code": "6230", "label": "6230 * Species-rich Nardus grasslands, on silicious substrates in mountain areas", "priority": True},
    {"code": "6240", "label": "6240 * Sub-Pannonic steppic grasslands", "priority": True},
    {"code": "6250", "label": "6250 * Pannonic loess steppic grasslands", "priority": True},
    {"code": "6260", "label": "6260 * Pannonic sand steppes", "priority": True},
    {"code": "6270", "label": "6270 * Fennoscandian lowland species-rich dry to mesic grasslands", "priority": True},
    {"code": "6280", "label": "6280 * Nordic alvar and precambrian calcareous flatrocks", "priority": True},
    {"code": "62A0", "label": "62A0 Eastern sub-Mediterranean dry grasslands", "priority": False},
    {"code": "62B0", "label": "62B0 * Serpentinophilous Mediterranean grasslands", "priority": True},
    {"code": "62C0", "label": "62C0 * Ponto-Sarmatic steppes", "priority": True},
    {"code": "62D0", "label": "62D0 Oro-Moesian acidophilous grasslands", "priority": False},
    {"code": "6310", "label": "6310 Dehesas with evergreen Quercus spp.", "priority": False},
    {"code": "6320", "label": "6320 Macaronesian laurel and heath tree formations", "priority": False},
    {"code": "6330", "label": "6330 Oro-Macaronesian heath", "priority": False},
    {"code": "6340", "label": "6340 Sub-alpine and alpine Pinus uncinata formations on gypsum or limestone", "priority": False},
    {"code": "6410", "label": "6410 Molinia meadows on calcareous, peaty or clayey-silt-laden soils", "priority": False},
    {"code": "6420", "label": "6420 Mediterranean tall humid grasslands of the Molinio-Holoschoenion", "priority": False},
    {"code": "6430", "label": "6430 Hydrophilous tall herb fringe communities of plains and of the montane to alpine levels", "priority": False},
    {"code": "6440", "label": "6440 Alluvial meadows of river valleys of the Cnidion dubii", "priority": False},
    {"code": "6450", "label": "6450 Northern boreal alluvial meadows", "priority": False},
    {"code": "6460", "label": "6460 Tundra bogs in Macaronesia", "priority": False},
    {"code": "6510", "label": "6510 Lowland hay meadows", "priority": False},
    {"code": "6520", "label": "6520 Mountain hay meadows", "priority": False},
    {"code": "6530", "label": "6530 * Fennoscandian wooded meadows", "priority": True},
    {"code": "6540", "label": "6540 Sub-Mediterranean grasslands of the Molinio-Holoschoenion", "priority": False},
    # 7xxx — Raised bogs and mires and fens
    {"code": "7110", "label": "7110 * Active raised bogs", "priority": True},
    {"code": "7120", "label": "7120 Degraded raised bogs still capable of natural regeneration", "priority": False},
    {"code": "7130", "label": "7130 Blanket bogs (* if active bog)", "priority": False},
    {"code": "7140", "label": "7140 Transition mires and quaking bogs", "priority": False},
    {"code": "7150", "label": "7150 Depressions on peat substrates of the Rhynchosporion", "priority": False},
    {"code": "7160", "label": "7160 Fennoscandian mineral-rich springs and springfens", "priority": False},
    {"code": "7170", "label": "7170 Blanket bogs", "priority": False},
    {"code": "7180", "label": "7180 * Tundra bogs", "priority": True},
    {"code": "7190", "label": "7190 * Tundra bogs in Macaronesia", "priority": True},
    {"code": "7210", "label": "7210 * Calcareous fens with Cladium mariscus and species of the Caricion davallianae", "priority": True},
    {"code": "7220", "label": "7220 * Petrifying springs with tufa formation", "priority": True},
    {"code": "7230", "label": "7230 Alkaline fens", "priority": False},
    {"code": "7240", "label": "7240 * Alpine pioneer formations of the Caricion bicoloris-atrofuscae", "priority": True},
    {"code": "7310", "label": "7310 Aapa mires", "priority": False},
    {"code": "7320", "label": "7320 Palsa mires", "priority": False},
    # 8xxx — Rocky habitats and caves
    {"code": "8110", "label": "8110 Siliceous scree of the montane to snow levels", "priority": False},
    {"code": "8120", "label": "8120 Calcareous and calcshist screes of the montane to alpine levels", "priority": False},
    {"code": "8130", "label": "8130 Western Mediterranean and thermophilous scree", "priority": False},
    {"code": "8140", "label": "8140 Eastern Mediterranean screes", "priority": False},
    {"code": "8150", "label": "8150 Medio-European upland siliceous screes", "priority": False},
    {"code": "8160", "label": "8160 * Medio-European calcareous scree of hill and montane levels", "priority": True},
    {"code": "8210", "label": "8210 Calcareous rocky slopes with chasmophytic vegetation", "priority": False},
    {"code": "8220", "label": "8220 Siliceous rocky slopes with chasmophytic vegetation", "priority": False},
    {"code": "8230", "label": "8230 Siliceous rock with pioneer vegetation of the Sedo-Scleranthion or of the Sedo albi-Veronicion dillenii", "priority": False},
    {"code": "8240", "label": "8240 * Limestone pavements", "priority": True},
    {"code": "8310", "label": "8310 Caves not open to the public", "priority": False},
    {"code": "8320", "label": "8320 Fields of lava and natural excavations", "priority": False},
    {"code": "8330", "label": "8330 Submerged or partially submerged sea caves", "priority": False},
    {"code": "8340", "label": "8340 Permanent glaciers", "priority": False},
    # 9xxx — Forests
    {"code": "9010", "label": "9010 * Western Taiga", "priority": True},
    {"code": "9020", "label": "9020 * Fennoscandian hemiboreal natural old broad-leaved deciduous forests", "priority": True},
    {"code": "9030", "label": "9030 * Natural forests of primary succession stages of landupheaval coast", "priority": True},
    {"code": "9040", "label": "9040 Nordic subalpine/subarctic forests with Betula pubescens ssp. czerepanovii", "priority": False},
    {"code": "9050", "label": "9050 Fennoscandian herb-rich forests with Picea abies", "priority": False},
    {"code": "9060", "label": "9060 Coniferous forests on, or connected to, glaciofluvial eskers", "priority": False},
    {"code": "9070", "label": "9070 Fennoscandian wooded pastures", "priority": False},
    {"code": "9080", "label": "9080 * Fennoscandian deciduous swamp woods", "priority": True},
    {"code": "9110", "label": "9110 Luzulo-Fagetum beech forests", "priority": False},
    {"code": "9120", "label": "9120 Atlantic acidophilous beech forests with Ilex and sometimes also Taxus in the shrublayer", "priority": False},
    {"code": "9130", "label": "9130 Asperulo-Fagetum beech forests", "priority": False},
    {"code": "9140", "label": "9140 Medio-European subalpine beech woods with Acer and Rumex arifolius", "priority": False},
    {"code": "9150", "label": "9150 Medio-European limestone beech forests of the Cephalanthero-Fagion", "priority": False},
    {"code": "9160", "label": "9160 Sub-Atlantic and medio-European oak or oak-hornbeam forests of the Carpinion betuli", "priority": False},
    {"code": "9170", "label": "9170 Galio-Carpinetum oak-hornbeam forests", "priority": False},
    {"code": "9180", "label": "9180 * Tilio-Acerion forests of slopes, screes and ravines", "priority": True},
    {"code": "9190", "label": "9190 Old acidophilous oak woods with Quercus robur on sandy plains", "priority": False},
    {"code": "91A0", "label": "91A0 Old sessile oak woods with Ilex and Blechnum in the British Isles", "priority": False},
    {"code": "91B0", "label": "91B0 Thermophilous Fraxinus angustifolia woods", "priority": False},
    {"code": "91C0", "label": "91C0 * Caledonian forest", "priority": True},
    {"code": "91D0", "label": "91D0 * Bog woodland", "priority": True},
    {"code": "91E0", "label": "91E0 * Alluvial forests with Alnus glutinosa and Fraxinus excelsior", "priority": True},
    {"code": "91F0", "label": "91F0 Riparian mixed forests of Quercus robur, Ulmus laevis and Ulmus minor, Fraxinus excelsior or Fraxinus angustifolia along the great rivers", "priority": False},
    {"code": "91G0", "label": "91G0 * Pannonic woods with Quercus petraea and Carpinus betulus", "priority": True},
    {"code": "91H0", "label": "91H0 * Pannonian woods with Quercus pubescens", "priority": True},
    {"code": "91I0", "label": "91I0 * Euro-Siberian steppic woods with Quercus spp.", "priority": True},
    {"code": "91J0", "label": "91J0 * Taxus baccata woods of the British Isles", "priority": True},
    {"code": "91K0", "label": "91K0 Illyrian Fagus sylvatica forests", "priority": False},
    {"code": "91L0", "label": "91L0 Illyrian oak-hornbeam forests", "priority": False},
    {"code": "91M0", "label": "91M0 Pannonian-Balkanic turkey oak – sessile oak forests", "priority": False},
    {"code": "91N0", "label": "91N0 * Pannonic inland sand dune thicket", "priority": True},
    {"code": "91P0", "label": "91P0 Holy Cross fir forest", "priority": False},
    {"code": "91Q0", "label": "91Q0 Western Carpathian calcicolous Pinus sylvestris forests", "priority": False},
    {"code": "91R0", "label": "91R0 Dinaric dolomite Scots pine forests", "priority": False},
    {"code": "91S0", "label": "91S0 * Western Pontic beech forests", "priority": True},
    {"code": "91T0", "label": "91T0 Central European lichen Scots pine forests", "priority": False},
    {"code": "91U0", "label": "91U0 Sarmatic steppe pine forests", "priority": False},
    {"code": "91V0", "label": "91V0 Dacian Beech forests", "priority": False},
    {"code": "91W0", "label": "91W0 Moesian beech forests", "priority": False},
    {"code": "91X0", "label": "91X0 * Dobrogean beech forests", "priority": True},
    {"code": "91Y0", "label": "91Y0 Dacian oak and hornbeam forests", "priority": False},
    {"code": "91Z0", "label": "91Z0 Moesian silver lime woods", "priority": False},
    {"code": "9210", "label": "9210 * Apennine beech forests with Taxus and Ilex", "priority": True},
    {"code": "9220", "label": "9220 * Apennine beech forests with Abies alba and beech forests with Abies nebrodensis", "priority": True},
    {"code": "9230", "label": "9230 Galicio-Portuguese oak woods with Quercus robur and Quercus pyrenaica", "priority": False},
    {"code": "9240", "label": "9240 Iberian Quercus faginea and Quercus canariensis woods", "priority": False},
    {"code": "9250", "label": "9250 Quercus trojana woods", "priority": False},
    {"code": "9260", "label": "9260 Castanea sativa woods", "priority": False},
    {"code": "9270", "label": "9270 Hellenic beech forests with Abies borisii-regis", "priority": False},
    {"code": "9280", "label": "9280 Quercus frainetto woods", "priority": False},
    {"code": "9290", "label": "9290 Cupressus forests", "priority": False},
    {"code": "92A0", "label": "92A0 Salix alba and Populus alba galleries", "priority": False},
    {"code": "92B0", "label": "92B0 Riparian formations on intermittent Mediterranean watercourses with Rhododendron ponticum, Salix and others", "priority": False},
    {"code": "92C0", "label": "92C0 Platanus orientalis and Liquidambar orientalis woods", "priority": False},
    {"code": "92D0", "label": "92D0 Southern riparian galleries and thickets", "priority": False},
    {"code": "9310", "label": "9310 Aegean Quercus brachyphylla forests", "priority": False},
    {"code": "9320", "label": "9320 Olea and Ceratonia forests", "priority": False},
    {"code": "9330", "label": "9330 Quercus suber forests", "priority": False},
    {"code": "9340", "label": "9340 Quercus ilex and Quercus rotundifolia forests", "priority": False},
    {"code": "9350", "label": "9350 Quercus macrolepis forests", "priority": False},
    {"code": "9360", "label": "9360 * Macaronesian laurel forests", "priority": True},
    {"code": "9370", "label": "9370 * Palm groves of Phoenix", "priority": True},
    {"code": "9380", "label": "9380 Forests of Ilex aquifolium", "priority": False},
    {"code": "9390", "label": "9390 * Scrub and low forest vegetation of Quercus alnifolia", "priority": True},
    {"code": "93A0", "label": "93A0 Woodlands with Quercus infectoria subsp. veneris", "priority": False},
    {"code": "9410", "label": "9410 Acidophilous Picea forests of the montane to alpine levels", "priority": False},
    {"code": "9420", "label": "9420 Alpine Larix decidua and/or Pinus cembra forests", "priority": False},
    {"code": "9430", "label": "9430 Subalpine and montane Pinus uncinata forests on gypsum and limestone substrates", "priority": False},
    {"code": "9510", "label": "9510 * Southern Apennine Abies alba forests", "priority": True},
    {"code": "9520", "label": "9520 Abies pinsapo forests", "priority": False},
    {"code": "9530", "label": "9530 * Sub-Mediterranean pine forests with endemic black pines", "priority": True},
    {"code": "9540", "label": "9540 Mediterranean pine forests with endemic Mesogean pines", "priority": False},
    {"code": "9550", "label": "9550 Canarian endemic pine forests", "priority": False},
    {"code": "9560", "label": "9560 * Endemic Macaronesian forests with Juniperus spp.", "priority": True},
    {"code": "9570", "label": "9570 * Tetraclinis articulata forests", "priority": True},
    {"code": "9580", "label": "9580 * Mediterranean Taxus baccata woods", "priority": True},
    {"code": "9590", "label": "9590 * Cedrus brevifolia forests", "priority": True},
    {"code": "95A0", "label": "95A0 High oro-Mediterranean pine forests", "priority": False},
]

# Minimal last-resort fallback for species — only used if ALL live sources fail.
_MINIMAL_FALLBACK_SPECIES = [
    # Mammals (Annex II/IV)
    {"scientific_name": "Canis lupus", "common_name": "Wolf", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Lynx lynx", "common_name": "Eurasian lynx", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Lynx pardinus", "common_name": "Iberian lynx", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Ursus arctos", "common_name": "Brown bear", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Lutra lutra", "common_name": "Otter", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Mustela lutreola", "common_name": "European mink", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Myotis bechsteinii", "common_name": "Bechstein's bat", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Myotis myotis", "common_name": "Greater mouse-eared bat", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Rhinolophus ferrumequinum", "common_name": "Greater horseshoe bat", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Rhinolophus hipposideros", "common_name": "Lesser horseshoe bat", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Barbastella barbastellus", "common_name": "Western barbastelle", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Miniopterus schreibersii", "common_name": "Schreibers' bat", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Spermophilus citellus", "common_name": "European ground squirrel", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Castor fiber", "common_name": "Eurasian beaver", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Phocoena phocoena", "common_name": "Harbour porpoise", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Tursiops truncatus", "common_name": "Bottlenose dolphin", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Monachus monachus", "common_name": "Mediterranean monk seal", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Bison bonasus", "common_name": "European bison", "group": "mammal", "annex": "II/IV"},
    {"scientific_name": "Vormela peregusna", "common_name": "Marbled polecat", "group": "mammal", "annex": "IV"},
    {"scientific_name": "Pteromys volans", "common_name": "Siberian flying squirrel", "group": "mammal", "annex": "IV"},
    # Amphibians (Annex II/IV)
    {"scientific_name": "Bombina bombina", "common_name": "Fire-bellied toad", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Bombina variegata", "common_name": "Yellow-bellied toad", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Triturus cristatus", "common_name": "Great crested newt", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Triturus carnifex", "common_name": "Italian crested newt", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Triturus karelinii", "common_name": "Southern crested newt", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Rana dalmatina", "common_name": "Agile frog", "group": "amphibian", "annex": "IV"},
    {"scientific_name": "Rana latastei", "common_name": "Italian agile frog", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Pelobates fuscus", "common_name": "Common spadefoot toad", "group": "amphibian", "annex": "IV"},
    {"scientific_name": "Pelobates cultripes", "common_name": "Western spadefoot toad", "group": "amphibian", "annex": "IV"},
    {"scientific_name": "Hyla arborea", "common_name": "European tree frog", "group": "amphibian", "annex": "IV"},
    {"scientific_name": "Discoglossus pictus", "common_name": "Painted frog", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Discoglossus sardus", "common_name": "Tyrrhenian painted frog", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Euproctus asper", "common_name": "Pyrenean brook salamander", "group": "amphibian", "annex": "IV"},
    {"scientific_name": "Chioglossa lusitanica", "common_name": "Golden-striped salamander", "group": "amphibian", "annex": "II/IV"},
    {"scientific_name": "Proteus anguinus", "common_name": "Olm", "group": "amphibian", "annex": "II/IV"},
    # Reptiles (Annex II/IV)
    {"scientific_name": "Emys orbicularis", "common_name": "European pond turtle", "group": "reptile", "annex": "II/IV"},
    {"scientific_name": "Testudo hermanni", "common_name": "Hermann's tortoise", "group": "reptile", "annex": "II/IV"},
    {"scientific_name": "Testudo graeca", "common_name": "Spur-thighed tortoise", "group": "reptile", "annex": "II/IV"},
    {"scientific_name": "Testudo marginata", "common_name": "Marginated tortoise", "group": "reptile", "annex": "II/IV"},
    {"scientific_name": "Caretta caretta", "common_name": "Loggerhead sea turtle", "group": "reptile", "annex": "II/IV"},
    {"scientific_name": "Dermochelys coriacea", "common_name": "Leatherback sea turtle", "group": "reptile", "annex": "IV"},
    {"scientific_name": "Lacerta agilis", "common_name": "Sand lizard", "group": "reptile", "annex": "IV"},
    {"scientific_name": "Lacerta viridis", "common_name": "Green lizard", "group": "reptile", "annex": "IV"},
    {"scientific_name": "Podarcis muralis", "common_name": "Common wall lizard", "group": "reptile", "annex": "IV"},
    {"scientific_name": "Vipera ursinii", "common_name": "Meadow viper", "group": "reptile", "annex": "II/IV"},
    {"scientific_name": "Coluber viridiflavus", "common_name": "Western whip snake", "group": "reptile", "annex": "IV"},
    {"scientific_name": "Elaphe quatuorlineata", "common_name": "Four-lined snake", "group": "reptile", "annex": "II/IV"},
    # Fish (Annex II)
    {"scientific_name": "Salmo salar", "common_name": "Atlantic salmon", "group": "fish", "annex": "II"},
    {"scientific_name": "Acipenser sturio", "common_name": "European sturgeon", "group": "fish", "annex": "II/IV"},
    {"scientific_name": "Acipenser naccarii", "common_name": "Adriatic sturgeon", "group": "fish", "annex": "II/IV"},
    {"scientific_name": "Hucho hucho", "common_name": "Huchen", "group": "fish", "annex": "II/IV"},
    {"scientific_name": "Aspius aspius", "common_name": "Asp", "group": "fish", "annex": "II"},
    {"scientific_name": "Barbus barbus", "common_name": "Barbel", "group": "fish", "annex": "V"},
    {"scientific_name": "Barbus meridionalis", "common_name": "Mediterranean barbel", "group": "fish", "annex": "II"},
    {"scientific_name": "Cottus gobio", "common_name": "Bullhead", "group": "fish", "annex": "II"},
    {"scientific_name": "Cobitis taenia", "common_name": "Spined loach", "group": "fish", "annex": "II"},
    {"scientific_name": "Misgurnus fossilis", "common_name": "Weatherfish", "group": "fish", "annex": "II"},
    {"scientific_name": "Rhodeus sericeus amarus", "common_name": "Bitterling", "group": "fish", "annex": "II"},
    {"scientific_name": "Romanichthys valsanicola", "common_name": "Asprete", "group": "fish", "annex": "II"},
    {"scientific_name": "Zingel zingel", "common_name": "Zingel", "group": "fish", "annex": "II/V"},
    {"scientific_name": "Alosa alosa", "common_name": "Allis shad", "group": "fish", "annex": "II/V"},
    {"scientific_name": "Alosa fallax", "common_name": "Twaite shad", "group": "fish", "annex": "II/V"},
    {"scientific_name": "Petromyzon marinus", "common_name": "Sea lamprey", "group": "fish", "annex": "II"},
    {"scientific_name": "Lampetra planeri", "common_name": "Brook lamprey", "group": "fish", "annex": "II"},
    {"scientific_name": "Lampetra fluviatilis", "common_name": "River lamprey", "group": "fish", "annex": "II/V"},
    # Invertebrates (Annex II/IV)
    {"scientific_name": "Cerambyx cerdo", "common_name": "Great capricorn beetle", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Lucanus cervus", "common_name": "Stag beetle", "group": "invertebrate", "annex": "II"},
    {"scientific_name": "Osmoderma eremita", "common_name": "Hermit beetle", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Rosalia alpina", "common_name": "Rosalia longicorn", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Maculinea arion", "common_name": "Large blue butterfly", "group": "invertebrate", "annex": "IV"},
    {"scientific_name": "Maculinea nausithous", "common_name": "Dusky large blue", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Maculinea teleius", "common_name": "Scarce large blue", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Euphydryas aurinia", "common_name": "Marsh fritillary", "group": "invertebrate", "annex": "II"},
    {"scientific_name": "Lycaena dispar", "common_name": "Large copper butterfly", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Coenagrion mercuriale", "common_name": "Southern damselfly", "group": "invertebrate", "annex": "II"},
    {"scientific_name": "Austropotamobius pallipes", "common_name": "White-clawed crayfish", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Margaritifera margaritifera", "common_name": "Freshwater pearl mussel", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Unio crassus", "common_name": "Thick shelled river mussel", "group": "invertebrate", "annex": "II"},
    {"scientific_name": "Vertigo moulinsiana", "common_name": "Desmoulin's whorl snail", "group": "invertebrate", "annex": "II"},
    {"scientific_name": "Vertigo angustior", "common_name": "Narrow-mouthed whorl snail", "group": "invertebrate", "annex": "II"},
    {"scientific_name": "Hirudo medicinalis", "common_name": "Medicinal leech", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Ophiogomphus cecilia", "common_name": "Green snaketail dragonfly", "group": "invertebrate", "annex": "II/IV"},
    {"scientific_name": "Graphoderus bilineatus", "common_name": "Graphoderus beetle", "group": "invertebrate", "annex": "II/IV"},
    # Plants (Annex II/IV)
    {"scientific_name": "Liparis loeselii", "common_name": "Fen orchid", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Dracocephalum austriacum", "common_name": "Austrian dragonhead", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Cypripedium calceolus", "common_name": "Lady's slipper orchid", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Luronium natans", "common_name": "Floating water plantain", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Apium repens", "common_name": "Creeping marshwort", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Aldrovanda vesiculosa", "common_name": "Waterwheel plant", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Angelica palustris", "common_name": "Marsh angelica", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Ostericum palustre", "common_name": "Marsh angelica", "group": "plant", "annex": "II"},
    {"scientific_name": "Marsilea quadrifolia", "common_name": "Four-leaf clover fern", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Meesia longiseta", "common_name": "Long-stalked thread-moss", "group": "plant", "annex": "II"},
    {"scientific_name": "Dryopteris corleyi", "common_name": "Corley's buckler-fern", "group": "plant", "annex": "IV"},
    {"scientific_name": "Spiranthes aestivalis", "common_name": "Summer lady's-tresses", "group": "plant", "annex": "II/IV"},
    {"scientific_name": "Himantoglossum adriaticum", "common_name": "Adriatic lizard orchid", "group": "plant", "annex": "II"},
    {"scientific_name": "Viola uliginosa", "common_name": "Bog violet", "group": "plant", "annex": "II"},
    {"scientific_name": "Galium sudeticum", "common_name": "Sudeten bedstraw", "group": "plant", "annex": "II"},
]

# Minimal last-resort fallback for birds — only used if ALL live sources fail.
_MINIMAL_FALLBACK_BIRDS = [
    # Herons, ibis, spoonbills
    {"scientific_name": "Botaurus stellaris", "common_name": "Eurasian bittern", "annex": "I"},
    {"scientific_name": "Ixobrychus minutus", "common_name": "Little bittern", "annex": "I"},
    {"scientific_name": "Nycticorax nycticorax", "common_name": "Black-crowned night heron", "annex": "I"},
    {"scientific_name": "Ardeola ralloides", "common_name": "Squacco heron", "annex": "I"},
    {"scientific_name": "Egretta garzetta", "common_name": "Little egret", "annex": "I"},
    {"scientific_name": "Egretta alba", "common_name": "Great white egret", "annex": "I"},
    {"scientific_name": "Ardea purpurea", "common_name": "Purple heron", "annex": "I"},
    {"scientific_name": "Plegadis falcinellus", "common_name": "Glossy ibis", "annex": "I"},
    {"scientific_name": "Platalea leucorodia", "common_name": "Eurasian spoonbill", "annex": "I"},
    # Pelicans
    {"scientific_name": "Pelecanus onocrotalus", "common_name": "Great white pelican", "annex": "I"},
    {"scientific_name": "Pelecanus crispus", "common_name": "Dalmatian pelican", "annex": "I"},
    # Flamingos
    {"scientific_name": "Phoenicopterus roseus", "common_name": "Greater flamingo", "annex": "I"},
    # Storks
    {"scientific_name": "Ciconia nigra", "common_name": "Black stork", "annex": "I"},
    {"scientific_name": "Ciconia ciconia", "common_name": "White stork", "annex": "I"},
    # Geese, ducks
    {"scientific_name": "Anser erythropus", "common_name": "Lesser white-fronted goose", "annex": "I"},
    {"scientific_name": "Branta leucopsis", "common_name": "Barnacle goose", "annex": "I"},
    {"scientific_name": "Branta ruficollis", "common_name": "Red-breasted goose", "annex": "I"},
    {"scientific_name": "Aythya nyroca", "common_name": "Ferruginous duck", "annex": "I"},
    {"scientific_name": "Marmaronetta angustirostris", "common_name": "Marbled teal", "annex": "I"},
    {"scientific_name": "Oxyura leucocephala", "common_name": "White-headed duck", "annex": "I"},
    # Eagles, hawks, kites, osprey
    {"scientific_name": "Pandion haliaetus", "common_name": "Osprey", "annex": "I"},
    {"scientific_name": "Pernis apivorus", "common_name": "European honey buzzard", "annex": "I"},
    {"scientific_name": "Milvus migrans", "common_name": "Black kite", "annex": "I"},
    {"scientific_name": "Milvus milvus", "common_name": "Red kite", "annex": "I"},
    {"scientific_name": "Haliaeetus albicilla", "common_name": "White-tailed eagle", "annex": "I"},
    {"scientific_name": "Gypaetus barbatus", "common_name": "Bearded vulture", "annex": "I"},
    {"scientific_name": "Neophron percnopterus", "common_name": "Egyptian vulture", "annex": "I"},
    {"scientific_name": "Gyps fulvus", "common_name": "Griffon vulture", "annex": "I"},
    {"scientific_name": "Aegypius monachus", "common_name": "Black vulture", "annex": "I"},
    {"scientific_name": "Circaetus gallicus", "common_name": "Short-toed eagle", "annex": "I"},
    {"scientific_name": "Circus aeruginosus", "common_name": "Western marsh harrier", "annex": "I"},
    {"scientific_name": "Circus cyaneus", "common_name": "Hen harrier", "annex": "I"},
    {"scientific_name": "Circus macrourus", "common_name": "Pallid harrier", "annex": "I"},
    {"scientific_name": "Circus pygargus", "common_name": "Montagu's harrier", "annex": "I"},
    {"scientific_name": "Accipiter brevipes", "common_name": "Levant sparrowhawk", "annex": "I"},
    {"scientific_name": "Buteo rufinus", "common_name": "Long-legged buzzard", "annex": "I"},
    {"scientific_name": "Aquila pomarina", "common_name": "Lesser spotted eagle", "annex": "I"},
    {"scientific_name": "Aquila clanga", "common_name": "Greater spotted eagle", "annex": "I"},
    {"scientific_name": "Aquila heliaca", "common_name": "Eastern imperial eagle", "annex": "I"},
    {"scientific_name": "Aquila adalberti", "common_name": "Spanish imperial eagle", "annex": "I"},
    {"scientific_name": "Aquila chrysaetos", "common_name": "Golden eagle", "annex": "I"},
    {"scientific_name": "Hieraaetus pennatus", "common_name": "Booted eagle", "annex": "I"},
    {"scientific_name": "Hieraaetus fasciatus", "common_name": "Bonelli's eagle", "annex": "I"},
    # Falcons
    {"scientific_name": "Falco naumanni", "common_name": "Lesser kestrel", "annex": "I"},
    {"scientific_name": "Falco columbarius", "common_name": "Merlin", "annex": "I"},
    {"scientific_name": "Falco vespertinus", "common_name": "Red-footed falcon", "annex": "I"},
    {"scientific_name": "Falco eleonorae", "common_name": "Eleonora's falcon", "annex": "I"},
    {"scientific_name": "Falco peregrinus", "common_name": "Peregrine falcon", "annex": "I"},
    # Rails and cranes
    {"scientific_name": "Porzana porzana", "common_name": "Spotted crake", "annex": "I"},
    {"scientific_name": "Porzana parva", "common_name": "Little crake", "annex": "I"},
    {"scientific_name": "Crex crex", "common_name": "Corncrake", "annex": "I"},
    {"scientific_name": "Grus grus", "common_name": "Common crane", "annex": "I"},
    # Waders
    {"scientific_name": "Himantopus himantopus", "common_name": "Black-winged stilt", "annex": "I"},
    {"scientific_name": "Recurvirostra avosetta", "common_name": "Pied avocet", "annex": "I"},
    {"scientific_name": "Glareola pratincola", "common_name": "Collared pratincole", "annex": "I"},
    {"scientific_name": "Charadrius alexandrinus", "common_name": "Kentish plover", "annex": "I"},
    {"scientific_name": "Pluvialis apricaria", "common_name": "European golden plover", "annex": "I"},
    {"scientific_name": "Philomachus pugnax", "common_name": "Ruff", "annex": "I"},
    {"scientific_name": "Gallinago media", "common_name": "Great snipe", "annex": "I"},
    {"scientific_name": "Numenius tenuirostris", "common_name": "Slender-billed curlew", "annex": "I"},
    {"scientific_name": "Tringa glareola", "common_name": "Wood sandpiper", "annex": "I"},
    {"scientific_name": "Larus melanocephalus", "common_name": "Mediterranean gull", "annex": "I"},
    {"scientific_name": "Larus audouinii", "common_name": "Audouin's gull", "annex": "I"},
    {"scientific_name": "Sterna nilotica", "common_name": "Gull-billed tern", "annex": "I"},
    {"scientific_name": "Sterna caspia", "common_name": "Caspian tern", "annex": "I"},
    {"scientific_name": "Sterna sandvicensis", "common_name": "Sandwich tern", "annex": "I"},
    {"scientific_name": "Sterna dougallii", "common_name": "Roseate tern", "annex": "I"},
    {"scientific_name": "Sterna hirundo", "common_name": "Common tern", "annex": "I"},
    {"scientific_name": "Sterna paradisaea", "common_name": "Arctic tern", "annex": "I"},
    {"scientific_name": "Sterna albifrons", "common_name": "Little tern", "annex": "I"},
    {"scientific_name": "Chlidonias hybridus", "common_name": "Whiskered tern", "annex": "I"},
    {"scientific_name": "Chlidonias niger", "common_name": "Black tern", "annex": "I"},
    # Owls and nightjars
    {"scientific_name": "Tyto alba", "common_name": "Barn owl", "annex": "I"},
    {"scientific_name": "Bubo bubo", "common_name": "Eurasian eagle owl", "annex": "I"},
    {"scientific_name": "Nyctea scandiaca", "common_name": "Snowy owl", "annex": "I"},
    {"scientific_name": "Glaucidium passerinum", "common_name": "Pygmy owl", "annex": "I"},
    {"scientific_name": "Strix uralensis", "common_name": "Ural owl", "annex": "I"},
    {"scientific_name": "Strix nebulosa", "common_name": "Great grey owl", "annex": "I"},
    {"scientific_name": "Asio flammeus", "common_name": "Short-eared owl", "annex": "I"},
    {"scientific_name": "Aegolius funereus", "common_name": "Tengmalm's owl", "annex": "I"},
    {"scientific_name": "Caprimulgus europaeus", "common_name": "European nightjar", "annex": "I"},
    # Woodpeckers
    {"scientific_name": "Dryocopus martius", "common_name": "Black woodpecker", "annex": "I"},
    {"scientific_name": "Dendrocopos leucotos", "common_name": "White-backed woodpecker", "annex": "I"},
    {"scientific_name": "Picoides tridactylus", "common_name": "Three-toed woodpecker", "annex": "I"},
    # Passerines and other
    {"scientific_name": "Lullula arborea", "common_name": "Woodlark", "annex": "I"},
    {"scientific_name": "Anthus campestris", "common_name": "Tawny pipit", "annex": "I"},
    {"scientific_name": "Acrocephalus melanopogon", "common_name": "Moustached warbler", "annex": "I"},
    {"scientific_name": "Acrocephalus paludicola", "common_name": "Aquatic warbler", "annex": "I"},
    {"scientific_name": "Sylvia nisoria", "common_name": "Barred warbler", "annex": "I"},
    {"scientific_name": "Ficedula parva", "common_name": "Red-breasted flycatcher", "annex": "I"},
    {"scientific_name": "Ficedula albicollis", "common_name": "Collared flycatcher", "annex": "I"},
    {"scientific_name": "Ficedula semitorquata", "common_name": "Semi-collared flycatcher", "annex": "I"},
    {"scientific_name": "Lanius collurio", "common_name": "Red-backed shrike", "annex": "I"},
    {"scientific_name": "Lanius minor", "common_name": "Lesser grey shrike", "annex": "I"},
    {"scientific_name": "Emberiza hortulana", "common_name": "Ortolan bunting", "annex": "I"},
    {"scientific_name": "Emberiza aureola", "common_name": "Yellow-breasted bunting", "annex": "I"},
    # Grebes, divers, shearwaters
    {"scientific_name": "Gavia stellata", "common_name": "Red-throated diver", "annex": "I"},
    {"scientific_name": "Gavia arctica", "common_name": "Black-throated diver", "annex": "I"},
    {"scientific_name": "Gavia immer", "common_name": "Great northern diver", "annex": "I"},
    {"scientific_name": "Podiceps auritus", "common_name": "Slavonian grebe", "annex": "I"},
    {"scientific_name": "Puffinus yelkouan", "common_name": "Yelkouan shearwater", "annex": "I"},
    {"scientific_name": "Calonectris diomedea", "common_name": "Cory's shearwater", "annex": "I"},
    {"scientific_name": "Hydrobates pelagicus", "common_name": "European storm petrel", "annex": "I"},
    {"scientific_name": "Oceanodroma leucorhoa", "common_name": "Leach's petrel", "annex": "I"},
    # Cormorants, gannets
    {"scientific_name": "Phalacrocorax aristotelis", "common_name": "European shag", "annex": "I"},
    {"scientific_name": "Phalacrocorax pygmeus", "common_name": "Pygmy cormorant", "annex": "I"},
    {"scientific_name": "Sula bassana", "common_name": "Northern gannet", "annex": "I"},
    # Auks
    {"scientific_name": "Fratercula arctica", "common_name": "Atlantic puffin", "annex": "I"},
    {"scientific_name": "Alca torda", "common_name": "Razorbill", "annex": "I"},
    {"scientific_name": "Uria aalge", "common_name": "Common guillemot", "annex": "I"},
    # Kingfisher, roller, bee-eater
    {"scientific_name": "Alcedo atthis", "common_name": "Common kingfisher", "annex": "I"},
    {"scientific_name": "Coracias garrulus", "common_name": "European roller", "annex": "I"},
    {"scientific_name": "Merops apiaster", "common_name": "European bee-eater", "annex": "I"},
]

BIOGEOGRAPHICAL_REGIONS = [
    {"code": "ALP", "name": "Alpine"},
    {"code": "ATL", "name": "Atlantic"},
    {"code": "BLS", "name": "Black Sea"},
    {"code": "BOR", "name": "Boreal"},
    {"code": "CON", "name": "Continental"},
    {"code": "MAC", "name": "Macaronesian"},
    {"code": "MED", "name": "Mediterranean"},
    {"code": "PAN", "name": "Pannonian"},
    {"code": "STE", "name": "Steppic"},
]

CONSERVATION_STATUSES = [
    {"code": "CR", "label": "Critically Endangered", "definition": "Facing an extremely high risk of extinction in the wild."},
    {"code": "EN", "label": "Endangered", "definition": "Facing a very high risk of extinction in the wild."},
    {"code": "VU", "label": "Vulnerable", "definition": "Facing a high risk of extinction in the wild."},
    {"code": "NT", "label": "Near Threatened", "definition": "Close to qualifying or likely to qualify for a threatened category in the near future."},
    {"code": "LC", "label": "Least Concern", "definition": "Does not qualify for a more at-risk category."},
    {"code": "DD", "label": "Data Deficient", "definition": "Inadequate information to make a direct or indirect assessment of risk of extinction."},
]


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def make_id(category: str, label: str) -> str:
    sanitized = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    sanitized = sanitized[:60]
    return f"{category}_{sanitized}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, **kwargs) -> requests.Response | None:
    try:
        resp = SESSION.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp
    except Exception as exc:
        logger.warning("HTTP GET failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def _parse_habitat_csv(text: str) -> list[dict]:
    """Parse a CSV/TSV response into habitat term dicts."""
    import csv
    import io
    terms = []
    for delimiter in (",", "\t", ";"):
        try:
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
            if not rows:
                continue
            for row in rows:
                code = row.get("CODE", row.get("code", row.get("Code", ""))).strip()
                name = row.get("NAME", row.get("name", row.get("Name",
                       row.get("HABITAT_NAME", row.get("HabitatName", ""))))).strip()
                if not code or not name or not re.match(r"^\d{4}", code):
                    continue
                label = f"{code} {name}"
                is_priority = "*" in name or row.get("PRIORITY", "").lower() in ("true", "1", "yes")
                terms.append({
                    "id": make_id("habitat", label),
                    "label": label,
                    "category": "habitat",
                    "definition": row.get("DESCRIPTION", row.get("description", "")),
                    "codes": [code],
                    "scientific_name": None,
                    "common_names": [],
                    "source_url": f"https://eunis.eea.europa.eu/habitats/{code}",
                    "directive": "habitats",
                    "annex": "I",
                    "conservation_status": None,
                    "priority": is_priority,
                })
            if terms:
                return terms
        except Exception:
            continue
    return terms


def _fetch_habitats_live() -> list[dict]:
    """Try multiple live sources for Annex I habitat types.

    Attempt order:
    1. EUNIS CSV export (direct download)
    2. EEA Reportnet CDR reference list
    3. EEA data catalogue download
    Returns [] if all sources fail.
    """
    candidate_urls = [
        "https://eunis.eea.europa.eu/export/habitats-csv",
        "https://eunis.eea.europa.eu/habitats/export?format=csv&habitatType=HD",
        "https://cdr.eionet.europa.eu/help/natura2000/habitats-annex1.csv",
        "https://eunis.eea.europa.eu/export/habitats-naturadirective-csv",
    ]

    for url in candidate_urls:
        try:
            resp = SESSION.get(url, timeout=30)
            if resp.status_code != 200:
                continue
            content_type = resp.headers.get("Content-Type", "")
            if "html" in content_type and len(resp.text) < 5000:
                continue  # likely an error page
            terms = _parse_habitat_csv(resp.text)
            if terms:
                logger.info("Fetched %d habitats from %s", len(terms), url)
                return terms
        except Exception as exc:
            logger.debug("Habitat source failed (%s): %s", url, exc)
        time.sleep(0.5)

    return []


def scrape_habitats() -> list[dict]:
    """Fetch Annex I habitat types with cascading fallbacks.

    Attempt order:
    1. Live fetch from EUNIS/EEA CSV endpoints
    2. Internal Annex I reference list (authoritative, based on official directive text)
    """
    terms = _fetch_habitats_live()

    if not terms:
        logger.info(
            "All live habitat sources unavailable — using internal reference list (%d habitats)",
            len(_FALLBACK_HABITATS),
        )
        for h in _FALLBACK_HABITATS:
            terms.append({
                "id": make_id("habitat", h["label"]),
                "label": h["label"],
                "category": "habitat",
                "definition": "",
                "codes": [h["code"]],
                "scientific_name": None,
                "common_names": [],
                "source_url": f"https://eunis.eea.europa.eu/habitats/{h['code']}",
                "directive": "habitats",
                "annex": "I",
                "conservation_status": None,
                "priority": h.get("priority", False),
            })
    return terms


# GBIF dataset keys for Habitats Directive annexes
_GBIF_DATASETS = {
    "II":  "2f845607-9b6a-4b31-b283-b87b89241e20",
    "IV":  "d1baf4e0-db47-4802-bce0-87881bc105d5",
    "V":   "0bc4fb0e-a8f2-4e77-a3cf-be1ed4c028bb",
}
_GBIF_BIRDS_ANNEX_I = "2bc74eff-e5e0-4000-b75b-77231934f3ad"
_GBIF_BASE = "https://api.gbif.org/v1"


def _gbif_class_to_group(kingdom: str, clazz: str) -> str:
    clazz = (clazz or "").lower()
    kingdom = (kingdom or "").lower()
    if clazz == "mammalia":
        return "mammal"
    if clazz == "aves":
        return "bird"
    if clazz == "amphibia":
        return "amphibian"
    if clazz in ("reptilia", "squamata", "testudines", "crocodilia"):
        return "reptile"
    if clazz in ("actinopterygii", "actinopteri", "petromyzontida",
                 "chondrichthyes", "cephalaspidomorphi"):
        return "fish"
    if kingdom == "plantae" or clazz in ("magnoliopsida", "liliopsida",
                                          "pinopsida", "polypodiopsida"):
        return "plant"
    if kingdom == "animalia":
        return "invertebrate"
    return "other"


def _fetch_gbif_checklist(dataset_key: str, annex: str, directive: str,
                           max_records: int = 2000) -> list[dict]:
    """Fetch all species from a GBIF checklist dataset with retry logic."""
    terms = []
    offset = 0
    limit = 100
    seen_ids: set[str] = set()
    max_retries = 3

    while offset < max_records:
        page_data = None
        for attempt in range(max_retries):
            try:
                resp = SESSION.get(
                    f"{_GBIF_BASE}/species/search",
                    params={"datasetKey": dataset_key, "limit": limit, "offset": offset},
                    timeout=30,
                )
                resp.raise_for_status()
                page_data = resp.json()
                break
            except Exception as exc:
                if attempt < max_retries - 1:
                    wait = 5 * (attempt + 1)
                    logger.warning(
                        "GBIF fetch failed (dataset=%s offset=%d, retry %d/%d in %ds): %s",
                        dataset_key[:8], offset, attempt + 1, max_retries, wait, exc,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "GBIF fetch gave up (dataset=%s offset=%d): %s",
                        dataset_key[:8], offset, exc,
                    )

        if page_data is None:
            break

        results = page_data.get("results", [])
        if not results:
            break

        for s in results:
            sci = (s.get("canonicalName") or s.get("scientificName") or "").strip()
            if not sci or sci in seen_ids:
                continue
            seen_ids.add(sci)

            kingdom = s.get("kingdom", "")
            clazz = s.get("class", "")
            group = _gbif_class_to_group(kingdom, clazz)

            # English only: some checklists carry e.g. Ukrainian names, which must not become the English label
            common = next((v["vernacularName"] for v in s.get("vernacularNames", [])
                           if v.get("language") in ("eng", "en") and v.get("vernacularName")), "")

            label = f"{sci} ({common})" if common else sci
            terms.append({
                "id": make_id("species", sci),
                "label": label,
                "category": "species",
                "definition": "",
                "codes": [],
                "scientific_name": sci,
                "common_names": [common] if common else [],
                "source_url": f"https://www.gbif.org/species/{s.get('key', '')}",
                "directive": directive,
                "annex": annex,
                "conservation_status": None,
                "taxonomic_group": group,
            })

        if page_data.get("endOfRecords", True):
            break
        offset += limit
        time.sleep(0.5)

    return terms


def _discover_gbif_hd_datasets() -> dict[str, str]:
    """Search GBIF for Habitats Directive checklist datasets and return annex→key mapping."""
    discovered: dict[str, str] = {}
    try:
        resp = SESSION.get(
            f"{_GBIF_BASE}/dataset/search",
            params={"q": "habitats directive annex", "type": "CHECKLIST", "limit": 20},
            timeout=20,
        )
        resp.raise_for_status()
        for ds in resp.json().get("results", []):
            title = (ds.get("title") or "").lower()
            key = ds.get("key", "")
            if not key:
                continue
            if "annex ii" in title or "annex 2" in title:
                discovered.setdefault("II", key)
            if "annex iv" in title or "annex 4" in title:
                discovered.setdefault("IV", key)
            if "annex v" in title or "annex 5" in title:
                discovered.setdefault("V", key)
        if discovered:
            logger.info("GBIF dataset discovery found: %s", discovered)
    except Exception as exc:
        logger.warning("GBIF dataset discovery failed: %s", exc)
    return discovered


def scrape_species() -> list[dict]:
    """Fetch Habitats Directive species (Annex II/IV/V) with cascading fallbacks.

    Attempt order:
    1. GBIF checklists using configured dataset keys (1101 + 1110 + 215 species)
    2. GBIF dataset key discovery (searches GBIF if configured keys return nothing)
    3. Minimal hardcoded last-resort list
    """
    terms: list[dict] = []
    seen_ids: set[str] = set()

    def _add_new(batch: list[dict]) -> int:
        added = 0
        for t in batch:
            if t["id"] not in seen_ids:
                seen_ids.add(t["id"])
                terms.append(t)
                added += 1
        return added

    # Level 1: GBIF with configured dataset keys
    tried_keys: set[str] = set()
    for annex, dataset_key in _GBIF_DATASETS.items():
        logger.info("Fetching Annex %s species from GBIF (%s...)", annex, dataset_key[:8])
        batch = _fetch_gbif_checklist(dataset_key, annex, "habitats")
        added = _add_new(batch)
        tried_keys.add(dataset_key)
        logger.info("Annex %s: %d new species (total so far: %d)", annex, added, len(terms))
        if batch:
            time.sleep(RATE_LIMIT)

    # Level 2: GBIF dataset key discovery (if all configured keys returned nothing)
    if not terms:
        logger.info("Configured GBIF keys returned no results — running discovery")
        discovered = _discover_gbif_hd_datasets()
        time.sleep(RATE_LIMIT)
        for annex, dataset_key in discovered.items():
            if dataset_key in tried_keys:
                continue
            logger.info("Retrying Annex %s with discovered GBIF key %s...", annex, dataset_key[:8])
            batch = _fetch_gbif_checklist(dataset_key, annex, "habitats")
            added = _add_new(batch)
            logger.info("Annex %s (discovered): %d new species", annex, added)
            if batch:
                time.sleep(RATE_LIMIT)

    # Level 3: Minimal hardcoded last resort
    if not terms:
        logger.warning(
            "All live species sources failed — using minimal fallback (%d species)",
            len(_MINIMAL_FALLBACK_SPECIES),
        )
        for s in _MINIMAL_FALLBACK_SPECIES:
            sci = s["scientific_name"]
            common = s["common_name"]
            terms.append({
                "id": make_id("species", sci),
                "label": f"{sci} ({common})",
                "category": "species",
                "definition": "",
                "codes": [],
                "scientific_name": sci,
                "common_names": [common],
                "source_url": f"https://eunis.eea.europa.eu/species/{sci.replace(' ', '+')}",
                "directive": "habitats",
                "annex": s.get("annex", "II"),
                "conservation_status": None,
                "taxonomic_group": s.get("group", ""),
            })

    return terms


def scrape_birds() -> list[dict]:
    """Fetch Birds Directive Annex I species with cascading fallbacks.

    Attempt order:
    1. GBIF Annex I checklist (263 species)
    2. GBIF Annex II Birds checklist (additional species)
    3. GBIF dataset key discovery
    4. Minimal hardcoded last-resort list
    """
    terms: list[dict] = []
    seen_ids: set[str] = set()

    def _add_new(batch: list[dict]) -> int:
        added = 0
        for t in batch:
            if t["id"] not in seen_ids:
                seen_ids.add(t["id"])
                terms.append(t)
                added += 1
        return added

    # Level 1: Primary GBIF Birds Directive Annex I checklist
    logger.info("Fetching Birds Directive Annex I from GBIF (%s...)", _GBIF_BIRDS_ANNEX_I[:8])
    batch = _fetch_gbif_checklist(_GBIF_BIRDS_ANNEX_I, "I", "birds", max_records=500)
    added = _add_new(batch)
    logger.info("Birds Annex I (GBIF primary): %d species", added)

    # Level 2: Birds Annex II checklist as supplementary source
    _GBIF_BIRDS_ANNEX_II = "314b4de6-1e07-4c1d-907e-719a4f8240f3"
    if not terms:
        time.sleep(RATE_LIMIT)
        logger.info("Trying Birds Directive Annex II GBIF dataset as fallback...")
        batch2 = _fetch_gbif_checklist(_GBIF_BIRDS_ANNEX_II, "II", "birds", max_records=500)
        added2 = _add_new(batch2)
        logger.info("Birds Annex II (GBIF supplementary): %d new species", added2)

    # Level 3: GBIF dataset discovery
    if not terms:
        logger.info("GBIF birds keys returned nothing — running discovery")
        try:
            resp = SESSION.get(
                f"{_GBIF_BASE}/dataset/search",
                params={"q": "birds directive annex", "type": "CHECKLIST", "limit": 10},
                timeout=20,
            )
            resp.raise_for_status()
            for ds in resp.json().get("results", []):
                title = (ds.get("title") or "").lower()
                key = ds.get("key", "")
                if key and key not in {_GBIF_BIRDS_ANNEX_I, _GBIF_BIRDS_ANNEX_II}:
                    if "birds directive" in title or "avian" in title:
                        logger.info("Discovered birds dataset: %s", key[:8])
                        batch3 = _fetch_gbif_checklist(key, "I", "birds", max_records=500)
                        _add_new(batch3)
                        if terms:
                            break
        except Exception as exc:
            logger.warning("GBIF birds discovery failed: %s", exc)

    # Level 4: Minimal hardcoded last resort
    if not terms:
        logger.warning(
            "All live bird sources failed — using minimal fallback (%d species)",
            len(_MINIMAL_FALLBACK_BIRDS),
        )
        for b in _MINIMAL_FALLBACK_BIRDS:
            sci = b["scientific_name"]
            common = b["common_name"]
            terms.append({
                "id": make_id("species", sci),
                "label": f"{sci} ({common})",
                "category": "species",
                "definition": "",
                "codes": [],
                "scientific_name": sci,
                "common_names": [common],
                "source_url": f"https://eunis.eea.europa.eu/species/{sci.replace(' ', '+')}",
                "directive": "birds",
                "annex": "I",
                "conservation_status": None,
                "taxonomic_group": "bird",
            })

    logger.info("Birds Directive: %d species loaded", len(terms))
    return terms


def build_regions() -> list[dict]:
    terms = []
    for r in BIOGEOGRAPHICAL_REGIONS:
        terms.append({
            "id": make_id("region", r["name"]),
            "label": r["name"],
            "category": "region",
            "definition": f"The {r['name']} biogeographical region as defined by the Habitats Directive.",
            "codes": [r["code"]],
            "scientific_name": None,
            "common_names": [],
            "source_url": "https://www.eea.europa.eu/themes/biodiversity/document-library/natura-2000/biogeographical-regions",
            "directive": None,
            "annex": None,
            "conservation_status": None,
        })
    return terms


def build_statuses() -> list[dict]:
    terms = []
    for s in CONSERVATION_STATUSES:
        terms.append({
            "id": make_id("status", s["code"]),
            "label": s["label"],
            "category": "status",
            "definition": s["definition"],
            "codes": [s["code"]],
            "scientific_name": None,
            "common_names": [],
            "source_url": "https://www.iucnredlist.org/resources/categories-and-criteria",
            "directive": None,
            "annex": None,
            "conservation_status": s["code"],
        })
    return terms


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def stratified_sample(terms: list[dict], limit: int) -> list[dict]:
    """Pick up to `limit` terms round-robin across categories and directives.

    Terms arrive grouped (all habitats first), so a plain slice would test only
    habitats. Interleaving also keeps later dry-run slices (the linker takes the
    first 20) representative of every category.
    """
    groups: dict[tuple, list[dict]] = {}
    for t in terms:
        groups.setdefault((t.get("category", ""), t.get("directive", "")), []).append(t)
    queues = list(groups.values())
    sample: list[dict] = []
    i = 0
    while len(sample) < limit and any(queues):
        q = queues[i % len(queues)]
        if q:
            sample.append(q.pop(0))
        i += 1
    return sample


def run_scraper(data_dir: str, dry_run: bool = False) -> list[dict]:
    """Scrape all Natura 2000 terms and save to {data_dir}/terms.json."""
    logger.info("Starting scraper (dry_run=%s)", dry_run)

    all_terms: list[dict] = []

    habitats = scrape_habitats()
    all_terms.extend(habitats)
    logger.info("Habitats: %d terms", len(habitats))

    species = scrape_species()
    all_terms.extend(species)
    logger.info("Habitats Directive species: %d terms", len(species))

    birds = scrape_birds()
    # Deduplicate species that appear in both lists
    existing_ids = {t["id"] for t in all_terms}
    new_birds = [b for b in birds if b["id"] not in existing_ids]
    all_terms.extend(new_birds)
    logger.info("Birds Directive species added: %d terms", len(new_birds))

    regions = build_regions()
    all_terms.extend(regions)
    logger.info("Biogeographical regions: %d terms", len(regions))

    statuses = build_statuses()
    all_terms.extend(statuses)
    logger.info("Conservation statuses: %d terms", len(statuses))

    if dry_run:
        limit = DRY_RUN_SIZE
        logger.info("Dry-run mode: limiting to %d terms, sampled across categories", limit)
        all_terms = stratified_sample(all_terms, limit)

    output_path = Path(data_dir) / "terms.json"
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(all_terms, fh, indent=2, ensure_ascii=False)

    logger.info("Saved %d terms to %s", len(all_terms), output_path)
    return all_terms


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Step 1: Scrape Natura 2000 terms.")
    parser.add_argument("--data-dir", default="./data", help="Output directory.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_scraper(args.data_dir, args.dry_run)

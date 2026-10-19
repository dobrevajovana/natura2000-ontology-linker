"""
Step 2 — Build OWL 2 Ontology

Takes the term register from Step 1 and builds a formal OWL 2 ontology
using rdflib. Exports to Turtle (.ttl) and JSON-LD formats.

Input:  {data_dir}/terms.json
Output: {data_dir}/ontology.ttl
        {data_dir}/ontology.jsonld
"""

import json
import logging
import re
from pathlib import Path

from rdflib import Graph, Namespace, RDF, RDFS, OWL, XSD, Literal, URIRef
from rdflib.namespace import SKOS, DC

logger = logging.getLogger("natura2000.ontology")

N2K = Namespace("https://ontology.natura2000.eu/")
OWL_NS = OWL

# ---------------------------------------------------------------------------
# Taxonomic group hints for species classification
# ---------------------------------------------------------------------------

MAMMAL_GENERA = {
    "canis", "lupus", "lynx", "ursus", "lutra", "myotis", "rhinolophus",
    "mustela", "meles", "castor", "crocidura", "erinaceus", "sorex",
    "microtus", "arvicola", "muscardinus", "glis", "spermophilus",
}
AMPHIBIAN_GENERA = {
    "bombina", "triturus", "rana", "bufo", "hyla", "salamandra", "proteus",
    "lissotriton", "ichthyosaura", "alytes", "discoglossus",
}
REPTILE_GENERA = {
    "emys", "testudo", "lacerta", "podarcis", "elaphe", "vipera", "natrix",
    "caretta", "dermochelys", "anguis",
}
FISH_GENERA = {
    "salmo", "acipenser", "barbus", "cobitis", "misgurnus", "rhodeus",
    "zingel", "gymnocephalus", "cottus", "thymallus",
}
PLANT_FAMILIES = {
    "orchid", "orchis", "dactylorhiza", "liparis", "dracocephalum",
    "cypripedium", "ophrys", "himantoglossum", "spiranthes",
}


def _species_subclass(term: dict) -> URIRef:
    directive = (term.get("directive") or "").lower()
    group = (term.get("taxonomic_group") or "").lower()
    sci = (term.get("scientific_name") or "").lower()
    genus = sci.split()[0] if sci else ""

    if directive == "birds" or group == "bird":
        return N2K.BirdSpecies
    if group == "mammal" or genus in MAMMAL_GENERA:
        return N2K.MammalSpecies
    if group == "amphibian" or genus in AMPHIBIAN_GENERA:
        return N2K.AmphibianSpecies
    if group == "reptile" or genus in REPTILE_GENERA:
        return N2K.ReptileSpecies
    if group == "fish" or genus in FISH_GENERA:
        return N2K.FishSpecies
    if group == "invertebrate" or group == "insect":
        return N2K.InvertebrateSpecies
    if group == "plant" or any(p in sci for p in PLANT_FAMILIES):
        return N2K.PlantSpecies
    return N2K.Species


def _habitat_subclass(term: dict) -> URIRef:
    codes = term.get("codes", [])
    if not codes:
        return N2K.Habitat
    code = str(codes[0]).strip()
    first = code[0] if code else ""
    mapping = {
        "1": N2K.CoastalHabitat,
        "2": N2K.DuneHabitat,
        "3": N2K.FreshwaterHabitat,
        "4": N2K.HeathHabitat,
        "5": N2K.ShrublandHabitat,
        "6": N2K.GrasslandHabitat,
        "7": N2K.BogHabitat,
        "8": N2K.RockyHabitat,
        "9": N2K.ForestHabitat,
    }
    return mapping.get(first, N2K.Habitat)


def _term_id(category: str, label: str) -> str:
    """Mirror of scraper.make_id(), so fixed individuals and scraped terms share URIs."""
    return f"{category}_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:60]


def _safe_uri_fragment(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text).strip("_")[:80]


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_ontology(terms: list[dict], data_dir: str) -> None:
    g = Graph()
    g.bind("n2k", N2K)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("rdf", RDF)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)
    g.bind("dc", DC)

    # ------------------------------------------------------------------
    # Ontology metadata
    # ------------------------------------------------------------------
    onto_uri = N2K[""]
    g.add((onto_uri, RDF.type, OWL.Ontology))
    g.add((onto_uri, RDFS.label, Literal("Natura 2000 Ontology", lang="en")))
    g.add((onto_uri, DC.description, Literal(
        "An OWL 2 ontology of Natura 2000 habitats, species, biogeographical regions, "
        "conservation statuses, and directives, linked to external knowledge bases.", lang="en")))
    g.add((onto_uri, OWL.versionInfo, Literal("1.0.0")))

    # ------------------------------------------------------------------
    # Class hierarchy
    # ------------------------------------------------------------------
    logger.info("Creating class hierarchy")

    def declare_class(uri: URIRef, label: str, comment: str = "", parent: URIRef = OWL.Thing):
        g.add((uri, RDF.type, OWL.Class))
        g.add((uri, RDFS.label, Literal(label, lang="en")))
        if comment:
            g.add((uri, RDFS.comment, Literal(comment, lang="en")))
        g.add((uri, RDFS.subClassOf, parent))

    declare_class(N2K.Term, "Natura 2000 Term",
                  "A term from the Natura 2000 network vocabulary.")

    # Habitat classes
    declare_class(N2K.Habitat, "Habitat",
                  "A habitat type as defined in Annex I of the Habitats Directive.", N2K.Term)
    habitat_subclasses = [
        (N2K.CoastalHabitat, "Coastal Habitat", "Marine and coastal habitat types (codes 1xxx)."),
        (N2K.DuneHabitat, "Dune Habitat", "Coastal and inland dune habitat types (codes 2xxx)."),
        (N2K.FreshwaterHabitat, "Freshwater Habitat", "Standing and running water habitats (codes 3xxx)."),
        (N2K.HeathHabitat, "Heath and Scrub Habitat", "Heathland and scrubland habitats (codes 4xxx)."),
        (N2K.ShrublandHabitat, "Shrubland Habitat", "Sclerophyllous scrub habitats (codes 5xxx)."),
        (N2K.GrasslandHabitat, "Grassland Habitat", "Natural and semi-natural grasslands (codes 6xxx)."),
        (N2K.BogHabitat, "Bog and Fen Habitat", "Raised bogs, mires, and fens (codes 7xxx)."),
        (N2K.RockyHabitat, "Rocky Habitat", "Rocky habitats and caves (codes 8xxx)."),
        (N2K.ForestHabitat, "Forest Habitat", "Forests of temperate Europe (codes 9xxx)."),
    ]
    for uri, label, comment in habitat_subclasses:
        declare_class(uri, label, comment, N2K.Habitat)

    # Disjoint habitat subclasses
    habitat_sub_uris = [uri for uri, _, _ in habitat_subclasses]
    for i, uri_a in enumerate(habitat_sub_uris):
        for uri_b in habitat_sub_uris[i + 1:]:
            g.add((uri_a, OWL.disjointWith, uri_b))

    # Species classes
    declare_class(N2K.Species, "Species",
                  "A species protected under the Habitats or Birds Directive.", N2K.Term)
    species_subclasses = [
        (N2K.BirdSpecies, "Bird Species", "Bird species listed in Annex I of the Birds Directive."),
        (N2K.MammalSpecies, "Mammal Species", "Mammal species listed in the Habitats Directive."),
        (N2K.AmphibianSpecies, "Amphibian Species", "Amphibian species listed in the Habitats Directive."),
        (N2K.ReptileSpecies, "Reptile Species", "Reptile species listed in the Habitats Directive."),
        (N2K.FishSpecies, "Fish Species", "Fish species listed in the Habitats Directive."),
        (N2K.InvertebrateSpecies, "Invertebrate Species", "Invertebrate species listed in the Habitats Directive."),
        (N2K.PlantSpecies, "Plant Species", "Plant species listed in the Habitats Directive."),
    ]
    for uri, label, comment in species_subclasses:
        declare_class(uri, label, comment, N2K.Species)

    # Disjoint species subclasses
    species_sub_uris = [uri for uri, _, _ in species_subclasses]
    for i, uri_a in enumerate(species_sub_uris):
        for uri_b in species_sub_uris[i + 1:]:
            g.add((uri_a, OWL.disjointWith, uri_b))

    # Supporting classes
    declare_class(N2K.BiogeographicalRegion, "Biogeographical Region",
                  "One of the nine biogeographical regions defined by the Habitats Directive.", N2K.Term)
    declare_class(N2K.ConservationStatus, "Conservation Status",
                  "An IUCN conservation status category used in Natura 2000 assessments.", N2K.Term)
    declare_class(N2K.Directive, "EU Directive",
                  "A European Union directive relevant to Natura 2000.", N2K.Term)

    # ------------------------------------------------------------------
    # Object properties
    # ------------------------------------------------------------------
    logger.info("Creating object properties")

    def declare_obj_prop(uri, label, domain, range_, comment=""):
        g.add((uri, RDF.type, OWL.ObjectProperty))
        g.add((uri, RDFS.label, Literal(label, lang="en")))
        g.add((uri, RDFS.domain, domain))
        g.add((uri, RDFS.range, range_))
        if comment:
            g.add((uri, RDFS.comment, Literal(comment, lang="en")))

    declare_obj_prop(N2K.foundInHabitat, "found in habitat",
                     N2K.Species, N2K.Habitat,
                     "Relates a species to a habitat type where it occurs.")
    declare_obj_prop(N2K.occursInRegion, "occurs in region",
                     N2K.Habitat, N2K.BiogeographicalRegion,
                     "Relates a habitat type to a biogeographical region where it occurs.")
    declare_obj_prop(N2K.hasConservationStatus, "has conservation status",
                     N2K.Term, N2K.ConservationStatus,
                     "Relates a term to its IUCN conservation status.")
    declare_obj_prop(N2K.protectedBy, "protected by",
                     N2K.Term, N2K.Directive,
                     "Relates a species or habitat to the EU directive that protects it.")

    # ------------------------------------------------------------------
    # Data properties
    # ------------------------------------------------------------------
    logger.info("Creating data properties")

    def declare_data_prop(uri, label, range_=XSD.string, comment=""):
        g.add((uri, RDF.type, OWL.DatatypeProperty))
        g.add((uri, RDFS.label, Literal(label, lang="en")))
        g.add((uri, RDFS.range, range_))
        if comment:
            g.add((uri, RDFS.comment, Literal(comment, lang="en")))

    declare_data_prop(N2K.euCode, "EU code", XSD.string, "The official EU code for a habitat or species.")
    declare_data_prop(N2K.scientificName, "scientific name", XSD.string)
    declare_data_prop(N2K.commonName, "common name", XSD.string)
    declare_data_prop(N2K.annexNumber, "annex number", XSD.string, "The Annex of the Directive listing this term.")
    declare_data_prop(N2K.sourceURL, "source URL", XSD.anyURI)

    # Annotation property
    g.add((N2K.isPriorityHabitat, RDF.type, OWL.AnnotationProperty))
    g.add((N2K.isPriorityHabitat, RDFS.label, Literal("is priority habitat", lang="en")))
    g.add((N2K.isPriorityHabitat, RDFS.range, XSD.boolean))

    # ------------------------------------------------------------------
    # Fixed individuals: regions
    # ------------------------------------------------------------------
    logger.info("Creating biogeographical region individuals")

    region_data = [
        ("Alpine", "ALP"), ("Atlantic", "ATL"), ("Black Sea", "BLS"),
        ("Boreal", "BOR"), ("Continental", "CON"), ("Macaronesian", "MAC"),
        ("Mediterranean", "MED"), ("Pannonian", "PAN"), ("Steppic", "STE"),
    ]
    # Same identifiers as the scraper's make_id(), so each region is one individual, not two
    for name, code in region_data:
        uri = N2K[_term_id("region", name)]
        g.add((uri, RDF.type, N2K.BiogeographicalRegion))
        g.add((uri, RDF.type, OWL.NamedIndividual))
        g.add((uri, RDFS.label, Literal(name, lang="en")))
        g.add((uri, N2K.euCode, Literal(code)))

    # Fixed individuals: conservation statuses
    logger.info("Creating conservation status individuals")
    status_data = [
        ("CR", "Critically Endangered"), ("EN", "Endangered"), ("VU", "Vulnerable"),
        ("NT", "Near Threatened"), ("LC", "Least Concern"), ("DD", "Data Deficient"),
    ]
    for code, label in status_data:
        uri = N2K[_term_id("status", code)]
        g.add((uri, RDF.type, N2K.ConservationStatus))
        g.add((uri, RDF.type, OWL.NamedIndividual))
        g.add((uri, RDFS.label, Literal(label, lang="en")))
        g.add((uri, N2K.euCode, Literal(code)))

    # Fixed individuals: directives
    logger.info("Creating directive individuals")
    g.add((N2K.HabitatsDirective, RDF.type, N2K.Directive))
    g.add((N2K.HabitatsDirective, RDF.type, OWL.NamedIndividual))
    g.add((N2K.HabitatsDirective, RDFS.label, Literal("Habitats Directive", lang="en")))
    g.add((N2K.HabitatsDirective, DC.identifier, Literal("92/43/EEC")))

    g.add((N2K.BirdsDirective, RDF.type, N2K.Directive))
    g.add((N2K.BirdsDirective, RDF.type, OWL.NamedIndividual))
    g.add((N2K.BirdsDirective, RDFS.label, Literal("Birds Directive", lang="en")))
    g.add((N2K.BirdsDirective, DC.identifier, Literal("2009/147/EC")))

    # ------------------------------------------------------------------
    # Term individuals from terms.json
    # ------------------------------------------------------------------
    logger.info("Creating %d term individuals", len(terms))

    for term in terms:
        tid = _safe_uri_fragment(term["id"])
        uri = N2K[tid]
        cat = term.get("category", "")

        if cat == "habitat":
            cls = _habitat_subclass(term)
        elif cat == "species":
            cls = _species_subclass(term)
        elif cat == "region":
            cls = N2K.BiogeographicalRegion
        elif cat == "status":
            cls = N2K.ConservationStatus
        else:
            cls = N2K.Term

        g.add((uri, RDF.type, cls))
        g.add((uri, RDF.type, OWL.NamedIndividual))
        g.add((uri, RDFS.label, Literal(term.get("label", tid), lang="en")))

        if term.get("definition"):
            g.add((uri, RDFS.comment, Literal(term["definition"], lang="en")))
            g.add((uri, SKOS.definition, Literal(term["definition"], lang="en")))

        for code in (term.get("codes") or []):
            g.add((uri, N2K.euCode, Literal(str(code))))

        if term.get("scientific_name"):
            g.add((uri, N2K.scientificName, Literal(term["scientific_name"])))

        for cn in (term.get("common_names") or []):
            if cn:
                g.add((uri, N2K.commonName, Literal(cn, lang="en")))

        if term.get("annex"):
            g.add((uri, N2K.annexNumber, Literal(str(term["annex"]))))

        if term.get("source_url"):
            g.add((uri, N2K.sourceURL, URIRef(term["source_url"])))

        if cat == "habitat" and term.get("priority"):
            g.add((uri, N2K.isPriorityHabitat, Literal(True, datatype=XSD.boolean)))

        # Link to directive
        directive = (term.get("directive") or "").lower()
        if directive == "habitats":
            g.add((uri, N2K.protectedBy, N2K.HabitatsDirective))
        elif directive == "birds":
            g.add((uri, N2K.protectedBy, N2K.BirdsDirective))

    # ------------------------------------------------------------------
    # Serialize
    # ------------------------------------------------------------------
    out_dir = Path(data_dir)

    ttl_path = out_dir / "ontology.ttl"
    g.serialize(destination=str(ttl_path), format="turtle")
    logger.info("Saved ontology to %s", ttl_path)

    jsonld_path = out_dir / "ontology.jsonld"
    g.serialize(destination=str(jsonld_path), format="json-ld", indent=2)
    logger.info("Saved ontology to %s", jsonld_path)

    logger.info(
        "Ontology built: %d triples, %d classes, %d individuals",
        len(g),
        sum(1 for _ in g.subjects(RDF.type, OWL.Class)),
        sum(1 for _ in g.subjects(RDF.type, OWL.NamedIndividual)),
    )


def run_ontology_builder(data_dir: str) -> None:
    data_path = Path(data_dir)
    with (data_path / "terms.json").open("r", encoding="utf-8") as fh:
        terms = json.load(fh)
    logger.info("Loaded %d terms from %s", len(terms), data_dir)
    build_ontology(terms, data_dir)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Step 2: Build OWL ontology.")
    parser.add_argument("--data-dir", default="./data")
    args = parser.parse_args()
    run_ontology_builder(args.data_dir)

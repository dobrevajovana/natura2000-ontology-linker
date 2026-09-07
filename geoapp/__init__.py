"""BioSample geo knowledge-graph application.

Ingests INSDC BioSample accessions, resolves them through NCBI E-utilities and
the EBI BioSamples API, extracts geolocation, links attributes to external
ontologies (ENVO, NCBITaxon, Wikidata) and archives (ENA, SRA, BioSample),
and serves an interactive map + knowledge-graph explorer.
"""

__version__ = "0.1.0"

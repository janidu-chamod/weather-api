"""Ingest a PDF using a pre-extracted report JSON (no Gemini API key needed)."""

import argparse
from pathlib import Path

from extractor import ExtractedReport, ingest_pdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="Path to the source PDF")
    parser.add_argument("report", help="Path to a JSON file matching ExtractedReport")
    parser.add_argument(
        "--source-system",
        required=True,
        choices=("dmc_sitrep", "nbro_landslide", "meteo_advisory"),
    )
    parser.add_argument("--source-url", default=None)
    args = parser.parse_args()

    report = ExtractedReport.model_validate_json(Path(args.report).read_text(encoding="utf-8"))
    result = ingest_pdf(
        args.pdf,
        source_system=args.source_system,
        source_url=args.source_url,
        report=report,
    )
    print(result)


if __name__ == "__main__":
    main()

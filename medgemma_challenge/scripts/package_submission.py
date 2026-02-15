#!/usr/bin/env python3
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent.parent
    submission_dir = root / "submission"
    output_zip = root / "submission_bundle.zip"

    if not submission_dir.exists():
        raise FileNotFoundError(f"Submission directory not found: {submission_dir}")

    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for path in submission_dir.rglob("*"):
            if path.is_file():
                zipf.write(path, arcname=path.relative_to(root))

    print(f"Created: {output_zip}")


if __name__ == "__main__":
    main()


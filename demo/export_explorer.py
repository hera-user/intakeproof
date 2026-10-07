"""Export the public explorer from real, verified saved bundles. No fabricated runs."""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intakeproof.verify import verify_bundle

assets = ROOT / 'docs/assets'
records = {}
for mode in ('baseline', 'reviewed'):
    bundle_path = assets / f'{mode}-evidence.zip'
    verify_bundle(bundle_path.read_bytes(), expected_source_sha256='96c98a6f890f86904a0395f217412170a00732e16bcb4e03bd4a99a0867fa105')
    with zipfile.ZipFile(bundle_path) as archive:
        records[mode] = json.loads(archive.read('audit.json'))
        (assets / f'{mode}-import.csv').write_bytes(archive.read('reviewed_import.csv'))
        original = archive.read('original.csv')
        if mode == 'reviewed':
            assert original == (assets / 'original.csv').read_bytes()
        else:
            (assets / 'original.csv').write_bytes(original)
assert records['baseline']['source'] == records['reviewed']['source']
(assets / 'runs.json').write_bytes((json.dumps(records, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
print(json.dumps({'baseline': records['baseline']['summary'], 'reviewed': records['reviewed']['summary'], 'source_preserved': True}))

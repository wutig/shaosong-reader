"""Package only the browser PDF reader for static hosting, including under a subpath."""

import argparse
from pathlib import Path
import re
import shutil


ROOT = Path(__file__).resolve().parent.parent
STYLES = (
    'styles.css', 'poetry.css', 'apple-design.css', 'navigation.css', 'theme.css',
    'hero.css', 'reader-theme.css', 'pdf-reader.css',
)
SCRIPTS = ('poetry.js', 'pdf-reader.js')
PUBLIC_ASSETS = (
    'poems.json',
    'assets/icons/app-icon-192.png',
    'assets/shaosong/hero-scene.png',
    'assets/shaosong/hero-figure.png',
    'assets/fonts/MaShanZheng-Regular.ttf',
    'assets/fonts/MaShanZheng-OFL.txt',
)
PDF_SUPPORT = {
    'cmaps': {'.bcmap'},
    'iccs': {'.icc'},
    'standard_fonts': {'.pfb', '.ttf'},
    'wasm': {'.js', '.wasm'},
}


def input_file(relative):
    path = ROOT / relative
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f'Invalid build input: {relative}')
    return path


def write_text(output, relative, body):
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-8', newline='\n')


def copy_file(output, source, destination=None):
    destination = output / (destination or source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(input_file(source), destination)


def build(output):
    # Do not include stale files from an earlier build or any study data.
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f'Choose an empty output directory: {output}')
    output.mkdir(parents=True, exist_ok=True)
    write_text(output, '.nojekyll', '')

    page = input_file('web/index.html').read_text(encoding='utf-8')
    page = re.sub(r'((?:src|href)=[\'\"])/(assets|vendor)/', r'\1\2/', page)
    write_text(output, 'index.html', page)
    for name in STYLES:
        style = input_file(f'web/{name}').read_text(encoding='utf-8')
        # Stylesheets are themselves served from assets/.
        style = style.replace("'/assets/", "'").replace('"/assets/', '"')
        write_text(output, f'assets/{name}', style)
    for name in SCRIPTS:
        script = input_file(f'web/{name}').read_text(encoding='utf-8')
        script = script.replace("fetch('/assets/poems.json'", "fetch('assets/poems.json'")
        script = script.replace(
            "new URL('/vendor/pdfjs/', location.origin)",
            "new URL('../vendor/pdfjs/', import.meta.url)",
        )
        write_text(output, f'assets/{name}', script)
    # The local server supplies this stylesheet based on files on the device.
    write_text(output, 'assets/local-theme.css', '/* Static site uses the bundled artwork. */\n')
    for name in PUBLIC_ASSETS:
        copy_file(output, f'web/{name}', f'assets/{name}')

    pdfjs = 'vendor/pdfjs'
    for name in ('pdf.min.mjs', 'pdf.worker.min.mjs', 'LICENSE', 'README.md'):
        copy_file(output, f'{pdfjs}/{name}')
    for directory, suffixes in PDF_SUPPORT.items():
        for path in sorted((ROOT / pdfjs / directory).iterdir()):
            if path.suffix in suffixes or path.name.startswith('LICENSE'):
                copy_file(output, path.relative_to(ROOT))
    for name in ('LICENSE', 'NOTICE'):
        copy_file(output, name)

    # Catch new HTML dependencies or an accidental root-relative asset URL before deployment.
    references = re.findall(r'(?:src|href)=[\'\"]((?:assets|vendor)/[^\'\"]+)', page)
    missing = [asset for asset in references if not (output / asset).is_file()]
    if missing:
        raise ValueError(f'Unpackaged reader assets: {", ".join(missing)}')
    for path in [output / 'index.html', *(output / 'assets' / name for name in STYLES + SCRIPTS)]:
        if re.search(r'[\'\"]/(assets|vendor)/', path.read_text(encoding='utf-8')):
            raise ValueError(f'Root-relative resource left in {path.relative_to(output)}')
    count = sum(path.is_file() for path in output.rglob('*'))
    print(f'Packaged {count} public PDF reader files in {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.runtime' / 'pages')
    options = parser.parse_args()
    build(options.output)

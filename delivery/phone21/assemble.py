"""GitHub-hosted fixed private draft assembly; no release publication/deletion."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import time

REPO = 'kingsalami8-a11y/keystream'
RELEASE = 438405002
BRANCH = 'refs/heads/delivery/public-phone21-20261108'
MANIFEST_SHA = 'c9f7ec5a71265d571052fa736e07e66653dfd195364f1cc3446cd418f4ba62a4'
APK_NAME = 'Keystream-phone-preview21-2026-11-07.apk'
APK_BYTES = 309747274
APK_SHA = 'd63eb86f2a11bc8fb34d9090c07f02588655a3987d9ea8091f7a99fbc2a561c4'

def sanitized(stderr):
    text = stderr.decode('utf-8', errors='replace') if isinstance(stderr, bytes) else str(stderr or '')
    text = re.sub(r'(?im)^.*authorization.*$', '[redacted authorization line]', text)
    text = re.sub(r'(?i)https?://\S+', '[redacted URL]', text)
    text = re.sub(r'\?[^\s]+', '[redacted query]', text)
    text = re.sub(r'(?i)\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|Bearer\s+\S+)', '[redacted token]', text)
    text = re.sub(r'\b[A-Za-z0-9_.-]{32,}\b', '[redacted long value]', text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)
    return text[:500]

def command(*args, output=None):
    try:
        result = subprocess.run(['gh', *args], stdout=output or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('GitHub operation timeout: ' + sanitized(error.stderr)) from None
    if result.returncode:
        raise RuntimeError('GitHub operation failed, exit ' + str(result.returncode) + ': ' + sanitized(result.stderr))
    return result.stdout

def api(route):
    return json.loads(command('api', route))

def validate_manifest(raw):
    assert len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == MANIFEST_SHA
    m = json.loads(raw)
    assert set(m) == {'schema', 'repository', 'repositoryId', 'releaseId', 'tag', 'branch', 'apk', 'chunkBytes', 'parts'}
    assert m['schema'] == 1 and m['repository'] == REPO and m['repositoryId'] == 1406194293
    assert m['releaseId'] == RELEASE and m['tag'] == 'phone-preview21-20261108'
    assert m['branch'] == BRANCH.removeprefix('refs/heads/')
    assert m['apk'] == {'name': APK_NAME, 'bytes': APK_BYTES, 'sha256': APK_SHA}
    assert m['chunkBytes'] == 2097152 and len(m['parts']) == 148
    for i, part in enumerate(m['parts']):
        assert set(part) == {'index', 'name', 'bytes', 'sha256'}
        assert part['index'] == i and part['name'] == f'phone21-part-{i:04d}.bin'
        assert type(part['bytes']) is int and 1 <= part['bytes'] <= m['chunkBytes']
        assert part['bytes'] == (2097152 if i < 148 - 1 else APK_BYTES - (148 - 1) * 2097152)
        assert re.fullmatch('[0-9a-f]{64}', part['sha256'])
    assert sum(p['bytes'] for p in m['parts']) == APK_BYTES
    return m

def assets():
    pages = json.loads(command('api', '--paginate', '--slurp', f'repos/{REPO}/releases/{RELEASE}/assets?per_page=100'))
    items = [a for page in pages for a in page]
    assert len({a['name'] for a in items}) == len(items), 'Duplicate asset names'
    return {a['name']: a for a in items}

def fixed_release():
    repo = api(f'repos/{REPO}')
    assert repo['private'] is False and repo['id'] == 1406194293
    release = api(f'repos/{REPO}/releases/{RELEASE}')
    assert release['id'] == RELEASE and release['tag_name'] == 'phone-preview21-20261108'
    assert release['draft'] is True and release['prerelease'] is True
    return release

def match(asset, size, digest):
    assert asset['state'] == 'uploaded' and asset['size'] == size
    assert asset['digest'] == 'sha256:' + digest

def download(part, asset, temporary=Path('part.tmp')):
    for attempt in range(1, 4):
        print(json.dumps({'part': part['index'], 'assetId': asset['id'], 'attempt': attempt}), flush=True)
        try:
            with temporary.open('wb') as out:
                # Binary bytes go only to this file, never to the terminal/log.
                command('api', '--allow-escape-sequences', f"repos/{REPO}/releases/assets/{asset['id']}",
                        '-H', 'Accept: application/octet-stream', output=out)
            if temporary.stat().st_size != part['bytes']:
                raise RuntimeError('Downloaded part size mismatch')
            chunk = temporary.read_bytes()
            if hashlib.sha256(chunk).hexdigest() != part['sha256']:
                raise RuntimeError('Downloaded part SHA256 mismatch')
            return chunk
        except RuntimeError as error:
            print(json.dumps({'part': part['index'], 'assetId': asset['id'],
                              'attempt': attempt, 'error': sanitized(str(error))}), flush=True)
            if attempt == 3:
                raise RuntimeError('Fixed part download failed after three attempts') from None
            time.sleep(attempt * 2)

def main():
    assert os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('GITHUB_REPOSITORY') == REPO and os.environ.get('GITHUB_REF') == BRANCH
    manifest = validate_manifest(Path('parts-manifest.json').read_bytes())
    fixed_release()
    remote = assets()
    if APK_NAME in remote:
        match(remote[APK_NAME], APK_BYTES, APK_SHA)
        print(json.dumps({'alreadyPresent': True, 'assetId': remote[APK_NAME]['id'], 'sha256': APK_SHA}))
        return
    for part in manifest['parts']:
        assert part['name'] in remote, 'Required chunk absent'
        match(remote[part['name']], part['bytes'], part['sha256'])
    whole = hashlib.sha256()
    total = 0
    with Path(APK_NAME).open('xb') as apk:
        for part in manifest['parts']:
            asset = remote[part['name']]
            chunk = download(part, asset)
            apk.write(chunk)
            whole.update(chunk)
            total += len(chunk)
    assert total == APK_BYTES and whole.hexdigest() == APK_SHA
    fixed_release()
    current = assets()
    if APK_NAME not in current:
        command('release', 'upload', manifest['tag'], APK_NAME, '--repo', REPO)
    current = assets()
    match(current[APK_NAME], APK_BYTES, APK_SHA)
    fixed_release()
    receipt = {'repository': REPO, 'releaseId': RELEASE, 'draft': True,
               'assetId': current[APK_NAME]['id'], 'name': APK_NAME,
               'bytes': APK_BYTES, 'sha256': APK_SHA, 'verifiedParts': 148,
               'url': current[APK_NAME]['browser_download_url'],
               'manifestSha256': MANIFEST_SHA, 'published': False, 'phoneActions': False}
    Path('SERVER-ASSEMBLY-VERIFIED.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))

if __name__ == '__main__':
    main()

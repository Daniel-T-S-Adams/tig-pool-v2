"""Validate recorded pool/worker pairs and serve only their checked installer bytes."""

import hashlib
import json
import re
import shlex

from .ledger import canonical
from .money import FundsError


POOL_REPOSITORY='https://github.com/Daniel-T-S-Adams/tig-pool-v2.git'
WORKER_REPOSITORY='https://github.com/Daniel-T-S-Adams/innopool-slave-v2.git'
CPU_TYPES={'aws_t3','aws_t3a','aws_t4g','aws_c7i','aws_c7a','aws_c7g','aws_m7i','aws_m7a','aws_m7g'}


def validate(manifest,build_commit,installer):
    if manifest is None:
        if build_commit is not None or installer is not None:raise ValueError('release metadata and installer must be configured together')
        return None
    try:
        value=json.loads(canonical(manifest))  # Keep a private snapshot, independent of caller mutations.
        if type(value['manifest_version']) is not int or value['manifest_version']!=1 or value['api_version']!='2.0':
            raise ValueError('unsupported release metadata version')
        for key,repository in (('pool',POOL_REPOSITORY),('worker',WORKER_REPOSITORY)):
            item=value[key]
            if (item['repository']!=repository or not re.fullmatch(r'[0-9a-f]{40}',item['commit'])
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',item['tag'])):
                raise ValueError('release must identify the expected fork, tag and full commit')
        if value['pool']['commit']!=build_commit:raise ValueError('release manifest differs from the running pool build')
        if type(value['worker']['state_version']) is not int or value['worker']['state_version']!=1:
            raise ValueError('incompatible worker evidence format')
        if not isinstance(installer,bytes) or not installer or len(installer)>1024*1024:
            raise ValueError('the paired installer bytes must be provided explicitly')
        digest=hashlib.sha256(installer).hexdigest()
        if value['worker']['installer_sha256']!=digest:raise ValueError('installer checksum differs from its paired worker release')
        images=value['runtime_images']
        if not isinstance(images,dict) or not images:raise ValueError('recorded runtime image digests are required')
        for challenge,image in images.items():
            if not re.fullmatch(r'c[0-9]+',challenge) or not re.fullmatch(
                r'ghcr\.io/tig-foundation/tig-monorepo/[a-z_]+/runtime@sha256:[0-9a-f]{64}',image):
                raise ValueError('runtime images must be explicit official digests')
        return value
    except (KeyError,TypeError) as error:raise ValueError('paired release metadata is incomplete or malformed') from error


def installation(manifest,pool_origin,resource,compute_type,workers):
    if (resource not in ('CPU','GPU') or (compute_type not in CPU_TYPES if resource=='CPU' else compute_type!='aws_g4dn')
            or type(workers) is not int or not 1<=workers<=4096):
        raise FundsError('choose a compatible CPU or GPU verification type and capacity from 1 to 4096')
    command='python3 install_worker_v2.py install \\\n  --pool '+shlex.quote(pool_origin)+' \\\n  --directory "$HOME/innopool-v2-member" \\\n  --resource '+resource+' --compute-type '+compute_type+' --workers '+str(workers)
    return {'installer_url':'/api/v2/install-worker','installer_sha256':manifest['worker']['installer_sha256'],
        'worker_commit':manifest['worker']['commit'],'command':command,
        'start_command':'"$HOME/innopool-v2-member/run"'}

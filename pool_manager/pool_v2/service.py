"""Explicit ASGI service factory; reads secrets only when the service starts."""

import hashlib
import json
import os
from pathlib import Path

from .api import Settings, create_app
from .chain import Network
from .database import Database
from . import pilot


def application():
    config = json.loads(Path(os.environ['POOL_V2_SERVICE_CONFIG']).read_text())
    for field in ('funds_enabled', 'work_enabled', 'settlement_enabled', 'require_pilot_limits'):
        if type(config.get(field, False)) is not bool:
            raise ValueError('service capability flags must be explicit booleans')
    # Runtime credentials are separate from the reviewable, nonsecret config.
    if 'database_dsn' in config or 'operator_token_sha256' in config:
        raise ValueError('database and operator credentials must use protected service configuration')
    database_dsn = os.environ['POOL_V2_DATABASE_DSN']
    operator_token = os.environ['POOL_V2_OPERATOR_TOKEN']
    if not 32 <= len(operator_token) <= 128:
        raise ValueError('invalid configured operator token length')
    required = config.pop('require_pilot_limits', False)
    api_origin = config.pop('tig_api_url')
    limits = pilot.require_service(Database(database_dsn), api_origin=api_origin,
        player_id=config['pool_player_id'], required=required)
    if limits and config['chain_id'] != limits['chain_id']:
        raise ValueError('pilot wallet-login chain differs from its recorded network')
    if config.get('custody_network') is not None:
        config['custody_network'] = Network(**config['custody_network'])
        network = config['custody_network']
        if limits and (network.chain_id, network.token, network.custody, network.decimals) != (
                limits['chain_id'], limits['token'], limits['pool_wallet'], 18):
            raise ValueError('pilot custody configuration differs from its recorded network')
    manifest = config.pop('release_manifest_file', None)
    installer = config.pop('worker_installer_file', None)
    if manifest: config['release_manifest'] = json.loads(Path(manifest).read_text())
    if installer: config['worker_installer'] = Path(installer).read_bytes()
    return create_app(Settings(database_dsn=database_dsn,
        operator_token_sha256=hashlib.sha256(operator_token.encode()).hexdigest(), **config))

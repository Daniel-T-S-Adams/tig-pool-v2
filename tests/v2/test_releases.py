from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from pool_manager.pool_v2.api import Settings,create_app
from pool_manager.pool_v2 import releases
from pool_manager.pool_v2.money import FundsError
from funds_helpers import DatabaseCase


INSTALLER=b'#!/usr/bin/env python3\n# Browser fixture only; never a production installer.\n'


def release_fixture(installer=INSTALLER):
    return {'manifest_version':1,'api_version':'2.0',
        'pool':{'repository':releases.POOL_REPOSITORY,'commit':'a'*40,'tag':'v2-pool-fixture'},
        'worker':{'repository':releases.WORKER_REPOSITORY,'commit':'b'*40,'tag':'v2-worker-fixture','state_version':1,
            'installer_sha256':hashlib.sha256(installer).hexdigest()},
        'runtime_images':{'c001':'ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:'+'c'*64}}


class ReleaseValidationTests(unittest.TestCase):
    def test_build_checksum_repository_tag_and_runtime_must_match(self):
        manifest=release_fixture()
        self.assertEqual(releases.validate(manifest,'a'*40,INSTALLER),manifest)
        with self.assertRaises(ValueError):releases.validate(manifest,'d'*40,INSTALLER)
        with self.assertRaises(ValueError):releases.validate(manifest,'a'*40,INSTALLER+b'changed')
        for change in ('repository','commit','tag','state','image'):
            invalid=deepcopy(manifest)
            if change=='repository':invalid['worker']['repository']='https://github.com/rootztigmod/innopool-slave.git'
            elif change=='commit':invalid['worker']['commit']='main'
            elif change=='tag':invalid['worker']['tag']='../branch'
            elif change=='state':invalid['worker']['state_version']=2
            else:invalid['runtime_images']['c001']='example:latest'
            with self.subTest(change=change),self.assertRaises(ValueError):releases.validate(invalid,'a'*40,INSTALLER)

    def test_install_commands_are_scoped_to_one_resource_and_quote_the_configured_origin(self):
        guide=releases.installation(release_fixture(),"https://pool.example;literal",'GPU','aws_g4dn',3)
        self.assertIn("--pool 'https://pool.example;literal'",guide['command'])
        self.assertIn('--resource GPU --compute-type aws_g4dn --workers 3',guide['command'])
        self.assertIn('"$HOME/innopool-v2-member"',guide['command'])
        self.assertNotIn('curl',guide['command'])
        for resource,compute,workers in [('CPU','aws_g4dn',1),('GPU','aws_c7a',1),('CPU','aws_c7a',0),('CPU','aws_c7a',True)]:
            with self.assertRaises(FundsError):releases.installation(release_fixture(),'https://pool.example',resource,compute,workers)


class ReleaseApiTests(DatabaseCase):
    def settings(self):
        return Settings(self.db.dsn,'https://pool.example',8453,'0'*64,release_manifest=release_fixture(),
            build_commit='a'*40,worker_installer=INSTALLER)

    def test_public_metadata_and_download_match_without_enabling_work_or_funds(self):
        settings=self.settings();app=create_app(settings);client=TestClient(app)
        expected=deepcopy(settings.release_manifest)
        settings.release_manifest['worker']['commit']='c'*40
        self.assertEqual(client.get('/api/v2/release').json(),expected)
        capabilities=client.get('/api/v2/capabilities').json()
        self.assertFalse(capabilities['funds_enabled']);self.assertFalse(capabilities['work_enabled'])
        self.assertEqual(capabilities['pool_commit'],'a'*40)
        self.assertEqual(capabilities['release_digest'],hashlib.sha256(releases.canonical(expected).encode()).hexdigest())
        script=client.get('/api/v2/install-worker')
        self.assertEqual(script.content,INSTALLER)
        self.assertEqual(script.headers['content-disposition'],'attachment; filename="install_worker_v2.py"')
        self.assertEqual(script.headers['cache-control'],'no-store')
        guide=client.get('/api/v2/worker-installation',params={'resource':'CPU','compute_type':'aws_c7g','workers':4})
        self.assertEqual(guide.status_code,200,guide.text)
        self.assertEqual(guide.json()['installer_sha256'],expected['worker']['installer_sha256'])
        self.assertEqual(client.get('/api/v2/worker-installation',params={'resource':'CPU','compute_type':'aws_g4dn'}).status_code,400)

    def test_unreleased_pool_cannot_serve_an_installer_or_claim_a_pair(self):
        client=TestClient(create_app(replace(self.settings(),release_manifest=None,worker_installer=None,build_commit=None)))
        for path in ('release','install-worker','worker-installation?resource=CPU&compute_type=aws_c7a'):
            self.assertEqual(client.get('/api/v2/'+path).status_code,503)
        self.assertIsNone(client.get('/api/v2/capabilities').json()['release_digest'])

    def test_paired_worker_bootstrap_accepts_the_actual_pool_metadata_and_its_served_bytes(self):
        checkout=os.environ.get('POOL_V2_WORKER_CHECKOUT')
        if not checkout:self.skipTest('set POOL_V2_WORKER_CHECKOUT to the paired worker revision')
        source=Path(checkout)/'tools/install_worker_v2.py'
        self.assertTrue(source.is_file(),'paired worker must include its pinned installer')
        script=source.read_bytes()
        settings=replace(self.settings(),worker_installer=script,release_manifest=release_fixture(script))
        client=TestClient(create_app(settings))
        spec=importlib.util.spec_from_file_location('paired_worker_installer',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        def fetch(pool,path):
            self.assertEqual(pool,settings.origin)
            response=client.get(path);self.assertEqual(response.status_code,200,response.text)
            return response.json()
        with patch.object(module,'fetch',side_effect=fetch):
            self.assertEqual(module.release_from_pool(settings.origin),settings.release_manifest)
        self.assertEqual(hashlib.sha256(client.get('/api/v2/install-worker').content).hexdigest(),
            settings.release_manifest['worker']['installer_sha256'])

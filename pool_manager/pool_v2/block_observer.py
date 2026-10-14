"""Durable, deduplicated block history and independent collection progress."""

from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
import gzip
import hashlib
import json
import uuid

from psycopg2.extras import Json, execute_values

from .database import lock
from .ledger import fingerprint
from .observation import canonical_json
from .protocol import ProtocolDataError, validate_snapshot


PAGE_TARGET = 8   # mean record digests per page; a changed record rewrites one page, and changes land in larger pages
ITEM_PAGE_TARGET = 32   # mean ids per page for the block's active-id lists, which change rarely
PAGE_MAXIMUM = 1024
BLOB_THRESHOLD = 8192   # larger inline values become one chunk, order preserved
GAP_POLICY = "bounded-zero-credit-v1"   # tolerated missing heights are zero credit, never fabricated


def _is_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _pages(values, target=PAGE_TARGET):
    """Split a sorted list into content-defined pages.

    A boundary falls after any element whose hash's leading bits select it,
    so one added or removed element changes one page wherever it sits.
    """
    pages, current = [], []
    for value in values:
        current.append(value)
        selector = int(hashlib.sha256(value.encode()).hexdigest()[:4], 16)
        if selector % target == 0 or len(current) >= PAGE_MAXIMUM:
            pages.append(current)
            current = []
    if current:
        pages.append(current)
    return pages


def _pack(observation, *, paged=True):
    """Encode an observation as a manifest tree plus content-addressed chunks.

    With ``paged`` (block observations), lists of records are stored in digest
    order as pages of digests. Records and pages are both deduplicated chunks,
    so an unchanged list costs nothing on the next block and a manifest holds
    a few page references per list. Without it (custody and report captures,
    where order can carry meaning), lists keep their original order inline.
    Returns the manifest, the chunks, and the observation the manifest
    replays to.
    """
    chunks = {}

    def store(raw):
        digest = hashlib.sha256(raw).hexdigest()
        if digest not in chunks:
            chunks[digest] = gzip.compress(raw, mtime=0)
        return digest

    def encode(value, path):
        if isinstance(value, dict):
            node, canonical = [], {}
            for key, item in sorted(value.items()):
                child, canonical[key] = encode(item, path + (key,))
                node.append([key, child])
            return {"dict": node}, canonical
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            encoded = [(store(canonical_json(item)), item) for item in value]
            if not paged:
                return {"records": [digest for digest, _ in encoded]}, list(value)
            ordered = sorted(encoded, key=lambda pair: pair[0])
            # A digest page is the raw 32-byte digests back to back; hex JSON
            # would double it and compress poorly at this size.
            pages = [store(b"".join(bytes.fromhex(digest) for digest in page))
                     for page in _pages([digest for digest, _ in ordered])]
            return {"pages": pages}, [item for _, item in ordered]
        if (paged and isinstance(value, list) and value and all(isinstance(item, str) for item in value)
                and path[-3:-1] == ("data", "active_ids")):
            # The block's active-id lists are validated as sets and the API
            # reorders them every block; store them sorted so they deduplicate.
            ordered = sorted(value)
            return {"items": [store(canonical_json(page)) for page in _pages(ordered, ITEM_PAGE_TARGET)]}, ordered
        if paged and len(raw := canonical_json(value)) > BLOB_THRESHOLD:
            return {"blob": store(raw)}, value
        return {"value": value}, value

    manifest, canonical = encode(observation, ())
    return manifest, chunks, canonical


def canonical_observation(observation):
    """The replayable form of an observation: record lists in digest order."""
    return _pack(observation)[2]


def _fetch(cursor, wanted):
    wanted = set(wanted)
    if not wanted:
        return {}
    cursor.execute("SELECT digest,compressed FROM observation_chunks WHERE digest=ANY(%s)", (sorted(wanted),))
    found = {}
    for row in cursor.fetchall():
        raw = gzip.decompress(bytes(row["compressed"]))
        if hashlib.sha256(raw).hexdigest() != row["digest"]:
            raise ProtocolDataError("stored observation chunk failed its checksum")
        found[row["digest"]] = raw
    if found.keys() != wanted:
        raise ProtocolDataError("stored observation is missing raw evidence")
    return found


def _page_digests(raw):
    if not raw or len(raw) % 32:
        raise ProtocolDataError("stored observation page is not a digest list")
    return [raw[index:index + 32].hex() for index in range(0, len(raw), 32)]


def manifest_chunk(manifest):
    """The manifest tree as one content-addressed, compressed chunk."""
    raw = canonical_json(manifest)
    return hashlib.sha256(raw).hexdigest(), gzip.compress(raw, mtime=0)


def _page_items(raw):
    listing = json.loads(raw)
    if not isinstance(listing, list) or not all(isinstance(item, str) for item in listing):
        raise ProtocolDataError("stored observation page is not a string list")
    return listing


def manifest_references(cursor, manifest):
    """Every chunk digest a stored manifest depends on; an expired manifest has none."""
    if "expired" in manifest:
        return set()
    if "compact_ref" in manifest:
        return {manifest["compact_ref"]}
    refs, pages = set(), set()
    if "manifest_ref" in manifest:
        refs.add(manifest["manifest_ref"])
        manifest = json.loads(_fetch(cursor, {manifest["manifest_ref"]})[manifest["manifest_ref"]])

    def collect(node):
        if "pages" in node:
            pages.update(node["pages"])
        elif "items" in node:
            refs.update(node["items"])
        elif "blob" in node:
            refs.add(node["blob"])
        elif "records" in node:
            refs.update(node["records"])
        elif "dict" in node:
            for _, child in node["dict"]:
                collect(child)
    collect(manifest)
    refs.update(pages)
    for raw in _fetch(cursor, pages).values():
        refs.update(_page_digests(raw))
    return refs


def _unpack(cursor, manifest):
    """Replay a manifest; both paged and legacy inline record lists are supported."""
    if "expired" in manifest:
        raise ProtocolDataError("block evidence expired under the retention policy on " + str(manifest["expired"].get("at")))
    if "compact_ref" in manifest:
        return json.loads(_fetch(cursor, {manifest["compact_ref"]})[manifest["compact_ref"]])
    if "manifest_ref" in manifest:
        manifest = json.loads(_fetch(cursor, {manifest["manifest_ref"]})[manifest["manifest_ref"]])
    page_refs, item_refs, blob_refs, record_refs = set(), set(), set(), set()

    def collect(node):
        if "pages" in node:
            page_refs.update(node["pages"])
        elif "items" in node:
            item_refs.update(node["items"])
        elif "blob" in node:
            blob_refs.add(node["blob"])
        elif "records" in node:
            record_refs.update(node["records"])
        elif "dict" in node:
            for _, child in node["dict"]:
                collect(child)
    collect(manifest)
    pages = {digest: _page_digests(raw) for digest, raw in _fetch(cursor, page_refs).items()}
    items = {digest: _page_items(raw) for digest, raw in _fetch(cursor, item_refs).items()}
    blobs = {digest: json.loads(raw) for digest, raw in _fetch(cursor, blob_refs).items()}
    for listing in pages.values():
        record_refs.update(listing)
    records = {digest: json.loads(raw) for digest, raw in _fetch(cursor, record_refs).items()}

    def decode(node):
        if "pages" in node:
            return [records[key] for page in node["pages"] for key in pages[page]]
        if "items" in node:
            return [item for page in node["items"] for item in items[page]]
        if "blob" in node:
            return blobs[node["blob"]]
        if "records" in node:
            return [records[key] for key in node["records"]]
        if "dict" in node:
            return {key: decode(child) for key, child in node["dict"]}
        return node["value"]
    return decode(manifest)


def _semantic(snapshot, observation):
    scores = defaultdict(list)
    for bundle in snapshot.bundles:
        scores[bundle.benchmark_id].append(bundle.quality)
    data={"block_id": snapshot.block_id, "previous": snapshot.previous_block_id,
        "height": snapshot.height, "round": snapshot.round, "timestamp": snapshot.timestamp,
        "config": (canonical_observation(observation["start"]["block"]["config"])
                   if observation.get('source_identity_version') == 2
                   else observation["start"]["block"]["config"]), "scores": scores,
        "precommits": snapshot.precommits, "algorithms": snapshot.algorithms, "challenges": snapshot.challenges,
        "qualifiers": sorted((*key, value) for key, value in snapshot.qualifiers.items())}
    if observation.get("pool_player_id"):
        player=observation["pool_player_id"]
        data["pool_player_id"]=player
        data["pool_feed"]=observation["players"].get(player,observation.get("pool_pending"))
    if snapshot.selection_references is not None:
        data["selection_references"] = [{"bundle": asdict(value["bundle"]),
            "hyperparameters": value["hyperparameters"]}
            for _, value in sorted(snapshot.selection_references.items())]
        data["source_semantic_digest"] = observation["source_semantic_digest"]
    return fingerprint(data)


class BlockStore:
    def __init__(self, database):
        self.database = database
        self._latest = None   # (block id, chunk references) of the newest block this store recorded

    def initialize(self, launch_height):
        if type(launch_height) is not int or launch_height < 0:
            raise ValueError("explicit nonnegative collection start height required")
        with self.database.transaction() as cursor:
            lock(cursor, "observation-stream")
            cursor.execute("SELECT launch_height FROM observation_stream WHERE name='tig'")
            row = cursor.fetchone()
            if row:
                if row["launch_height"] != launch_height:
                    raise ProtocolDataError("collection start cannot change after initialization")
                return
            cursor.execute("""INSERT INTO observation_stream VALUES ('tig',%s,%s,%s)""",
                           (launch_height, launch_height-1, launch_height-1))

    def record(self, observation, *, collector, metadata=None, error=None):
        """Store available evidence even when it cannot establish a complete block."""
        if not collector:
            raise ValueError("collector identity is required")
        snapshot = None
        try:
            snapshot = validate_snapshot(observation)
            blocks_per_round = observation["start"]["block"]["config"]["rounds"]["blocks_per_round"]
            if type(blocks_per_round) is not int or blocks_per_round <= 0:
                raise ProtocolDataError("round length is unavailable")
            if snapshot.height // blocks_per_round + 1 != snapshot.round:
                raise ProtocolDataError("block/round boundary mapping differs from the pinned adapter")
        except (ProtocolDataError, KeyError, TypeError) as exc:
            error = error or str(exc)
        source_observation = observation
        if snapshot and not error and observation.get("pool_player_id"):
            from .compact import compact_observation, metadata_summary
            observation = compact_observation(observation)
            snapshot = validate_snapshot(observation)
            metadata = metadata_summary(metadata or {}) if "requests" in (metadata or {}) else metadata
        block = observation.get("start", {}).get("block", {})
        height, block_id = block.get("details", {}).get("height"), block.get("id")
        if type(height) is not int or height < 0:
            height = None
        if not isinstance(block_id, str):
            block_id = None
        if observation.get("format") == "pool-network-v1":
            # One small blob avoids a per-field manifest and millions of tiny
            # chunks. The entire retained projection is checksummed together.
            raw = canonical_json(observation)
            reference = hashlib.sha256(raw).hexdigest()
            chunks, canonical = {reference: gzip.compress(raw, mtime=0)}, observation
            manifest = {"compact_ref": reference}
        else:
            manifest, chunks, canonical = _pack(observation)
            reference, compressed = manifest_chunk(manifest)
            chunks[reference] = compressed   # tracked like every other chunk
            manifest = {"manifest_ref": reference}
        identity = uuid.uuid4()
        with self.database.transaction() as cursor:
            lock(cursor, "observation-stream")
            cursor.execute("SELECT * FROM observation_stream WHERE name='tig' FOR UPDATE")
            stream = cursor.fetchone()
            if not stream:
                raise ProtocolDataError("initialize collection start before recording blocks")
            cursor.execute("SELECT digest FROM observation_chunks WHERE digest=ANY(%s)", (list(chunks),))
            present = {row["digest"] for row in cursor.fetchall()}
            missing = [(key, data) for key, data in chunks.items() if key not in present]
            if missing:
                execute_values(cursor, "INSERT INTO observation_chunks(digest,compressed) VALUES %s ON CONFLICT DO NOTHING", missing, page_size=300)
            if snapshot and not error:
                # The stored block replays in canonical order, so its digest is
                # taken over that form. Blocks recorded before paged manifests
                # kept the API's order; accept their digest for the same block.
                semantic = _semantic(snapshot, canonical)
                accepted = {semantic, _semantic(snapshot, observation), *observation.get("source_semantic_digests", []),
                            *observation.get('compatible_projection_digests', [])}
                cursor.execute("SELECT id,semantic_digest FROM observed_blocks WHERE height=%s OR id=%s", (height, block_id))
                previous = cursor.fetchall()
                if source_observation.get('format') is None:
                    for row in previous:
                        if row['id'] != block_id or row['semantic_digest'] in accepted:
                            continue
                        cursor.execute('SELECT a.manifest FROM observed_blocks b JOIN capture_attempts a ON a.id=b.attempt_id WHERE b.id=%s', (block_id,))
                        stored_manifest = cursor.fetchone()['manifest']
                        if 'expired' in stored_manifest:
                            continue
                        stored = _unpack(cursor, stored_manifest)
                        if stored.get('format') != 'pool-network-v1' or stored.get('source_identity_version', 1) != 1:
                            continue
                        # Recreate an immutable v1 compact identity using its
                        # original header order, after checking header values.
                        if canonical_observation(stored['start']['block']['config']) != canonical_observation(source_observation['start']['block']['config']):
                            continue
                        restored = deepcopy(source_observation)
                        for anchor in ('start', 'end'):
                            restored[anchor]['block']['config'] = deepcopy(stored[anchor]['block']['config'])
                        from .compact import compact_observation
                        old = compact_observation(restored, identity_version=1)
                        candidate = _semantic(validate_snapshot(old), old)
                        if candidate == row['semantic_digest']:
                            accepted.add(candidate)
                if any(row["id"] != block_id or row["semantic_digest"] not in accepted for row in previous):
                    error = "conflicting block identity or accounting evidence at a captured height"
                    cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('conflicting-block',%s,%s)",
                                   (height, Json({"block_id": block_id, "collector": collector})))
                # Check both neighbors, including a later block recovered first.
                cursor.execute("SELECT id,height,previous_id FROM observed_blocks WHERE height IN (%s,%s)", (height-1, height+1))
                for neighbor in cursor.fetchall():
                    if ((neighbor["height"] == height-1 and neighbor["id"] != snapshot.previous_block_id)
                            or (neighbor["height"] == height+1 and neighbor["previous_id"] != block_id)):
                        error = "captured block does not link to its stored neighbor"
                        cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('conflicting-block',%s,%s)",
                                       (height, Json({"block_id": block_id, "neighbor": neighbor["id"]})))
            cursor.execute("""INSERT INTO capture_attempts(id,collector,block_id,height,manifest,metadata,error)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""", (identity, collector, block_id, height,
                Json(manifest), Json(metadata or {}), error))
            if height is not None and height > stream["latest_seen_height"]:
                cursor.execute("UPDATE observation_stream SET latest_seen_height=%s WHERE name='tig'", (height,))
            cursor.execute("SELECT min(height) AS height FROM unresolved_observation_conflicts WHERE kind='conflicting-block' AND height >= %s",
                           (stream["launch_height"],))
            conflict_height = cursor.fetchone()["height"]
            if conflict_height is not None and conflict_height <= stream["contiguous_height"]:
                stream["contiguous_height"] = conflict_height - 1
                cursor.execute("UPDATE observation_stream SET contiguous_height=%s WHERE name='tig'", (conflict_height-1,))
            if snapshot and not error:
                cursor.execute("""INSERT INTO observed_blocks
                    (id,height,previous_id,round,timestamp,blocks_per_round,semantic_digest,attempt_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (block_id, height, snapshot.previous_block_id, snapshot.round, snapshot.timestamp,
                     blocks_per_round, semantic, identity))
                self._advance(cursor, stream["contiguous_height"])
                cursor.execute('''SELECT a.manifest FROM observed_blocks b
                    JOIN capture_attempts a ON a.id=b.attempt_id WHERE b.id=%s''', (block_id,))
                actual_refs = manifest_references(cursor, cursor.fetchone()['manifest'])
                self._track(cursor, height, block_id, actual_refs)
                extra = sorted(set(chunks) - actual_refs)
                if extra:
                    cursor.execute('SELECT max(height) AS height FROM observed_blocks')
                    last_height = cursor.fetchone()['height']
                    execute_values(cursor, '''INSERT INTO chunk_last_ref(digest,height) VALUES %s
                        ON CONFLICT (digest) DO UPDATE SET height=GREATEST(chunk_last_ref.height,EXCLUDED.height)''',
                        [(digest, last_height) for digest in extra], page_size=500)
            else:
                cursor.execute("INSERT INTO observation_alerts(kind,height,details) VALUES ('incomplete-capture',%s,%s)",
                               (height, Json({"attempt_id": str(identity), "error": error})))
        return {"attempt_id": str(identity), "complete": snapshot is not None and not error,
                "block_id": block_id, "height": height, "error": error}

    def _track(self, cursor, height, block_id, refs):
        """Keep chunk_last_ref: rows only for chunks the newest block no longer references."""
        cursor.execute("SELECT 1 FROM observed_blocks WHERE height>%s LIMIT 1", (height,))
        if cursor.fetchone():
            # A recovered older block: its references may outlive their recorded last use.
            cursor.execute("UPDATE chunk_last_ref SET height=GREATEST(height,%s) WHERE digest=ANY(%s)", (height, sorted(refs)))
            return
        cursor.execute("SELECT id,height FROM observed_blocks WHERE height<%s ORDER BY height DESC LIMIT 1", (height,))
        previous = cursor.fetchone()
        if previous:
            if self._latest and self._latest[0] == previous["id"]:
                previous_refs = self._latest[1]
            else:
                cursor.execute("SELECT a.manifest FROM observed_blocks b JOIN capture_attempts a ON a.id=b.attempt_id WHERE b.id=%s", (previous["id"],))
                previous_refs = manifest_references(cursor, cursor.fetchone()["manifest"])
            dropped = sorted(previous_refs - refs)
            if dropped:
                execute_values(cursor, """INSERT INTO chunk_last_ref(digest,height) VALUES %s
                    ON CONFLICT (digest) DO UPDATE SET height=GREATEST(chunk_last_ref.height,EXCLUDED.height)""",
                    [(digest, previous["height"]) for digest in dropped], page_size=500)
            cursor.execute("DELETE FROM chunk_last_ref WHERE digest=ANY(%s)", (sorted(refs),))
        self._latest = (block_id, frozenset(refs))

    def _advance(self, cursor, contiguous):
        cursor.execute("SELECT id FROM observed_blocks WHERE height=%s", (contiguous,))
        row = cursor.fetchone()
        previous_id = row["id"] if row else None
        cursor.execute("""SELECT id,height,previous_id FROM observed_blocks b WHERE height>%s
            AND NOT EXISTS (SELECT 1 FROM unresolved_observation_conflicts a WHERE a.kind='conflicting-block' AND a.height=b.height)
            ORDER BY height""", (contiguous,))
        for row in cursor.fetchall():
            if row["height"] != contiguous + 1 or (previous_id and row["previous_id"] != previous_id):
                break
            contiguous, previous_id = row["height"], row["id"]
        cursor.execute("UPDATE observation_stream SET contiguous_height=%s WHERE name='tig'", (contiguous,))

    def read(self, block_id):
        with self.database.transaction() as cursor:
            cursor.execute("""SELECT a.manifest,b.semantic_digest,b.height,c.compressed_payload,
                c.payload_sha256,c.projection_digest,c.source_semantic_digest,c.expired_at
                FROM observed_blocks b JOIN capture_attempts a ON a.id=b.attempt_id
                LEFT JOIN compact_block_copies c ON c.block_id=b.id WHERE b.id=%s""", (block_id,))
            row = cursor.fetchone()
            if not row:
                raise ProtocolDataError("complete block is not available in the pool archive")
            cursor.execute("SELECT 1 FROM unresolved_observation_conflicts WHERE kind='conflicting-block' AND height=%s LIMIT 1", (row["height"],))
            if cursor.fetchone():
                raise ProtocolDataError("conflicting observation requires investigation before use")
            if row['source_semantic_digest'] is not None:
                if row['expired_at'] is not None:
                    raise ProtocolDataError('compact block evidence expired under the retention policy')
                if row['source_semantic_digest'] != row['semantic_digest']:
                    raise ProtocolDataError('compact replacement differs from the original block identity')
                raw = gzip.decompress(bytes(row['compressed_payload']))
                if hashlib.sha256(raw).hexdigest() != row['payload_sha256']:
                    raise ProtocolDataError('compact replacement failed its payload checksum')
                observation = json.loads(raw)
            else:
                observation = _unpack(cursor, row["manifest"])
        snapshot = validate_snapshot(observation)
        if _semantic(snapshot, observation) != (row['projection_digest'] or row["semantic_digest"]):
            raise ProtocolDataError("stored block replay differs from its validated evidence")
        return observation, snapshot

    def status(self):
        with self.database.transaction() as cursor:
            cursor.execute("SELECT * FROM observation_stream WHERE name='tig'")
            result = dict(cursor.fetchone() or {})
            if not result:
                return {"initialized": False}
            cursor.execute("""SELECT expected.height FROM generate_series(%s,%s) AS expected(height)
                LEFT JOIN observed_blocks b ON b.height=expected.height
                WHERE b.id IS NULL ORDER BY expected.height LIMIT 100""",
                (result["contiguous_height"]+1, result["latest_seen_height"]))
            missing = [row["height"] for row in cursor.fetchall()]
            cursor.execute("""SELECT w.height FROM observation_gap_waivers w
                LEFT JOIN observed_blocks b ON b.height=w.height
                WHERE b.id IS NULL ORDER BY w.height""")
            waived = {row["height"] for row in cursor.fetchall()}
            result["missing_heights"] = missing
            result["waived_missing_heights"] = [height for height in missing if height in waived]
            result["unresolved_missing_heights"] = [height for height in missing if height not in waived]
            cursor.execute("SELECT DISTINCT height FROM unresolved_observation_conflicts WHERE kind='conflicting-block' ORDER BY height LIMIT 100")
            result["conflicting_heights"] = [row["height"] for row in cursor.fetchall()]
            cursor.execute('''SELECT DISTINCT a.height FROM observation_alerts a
                JOIN observation_conflict_resolutions r ON r.alert_id=a.id ORDER BY a.height LIMIT 100''')
            result['resolved_conflicting_heights'] = [row['height'] for row in cursor.fetchall()]
            cursor.execute("SELECT max(timestamp) AS latest_timestamp FROM observed_blocks")
            result["latest_timestamp"] = cursor.fetchone()["latest_timestamp"]
            policy = self.gap_policy(cursor=cursor)
            result["missing_block_policy"] = {key: policy[key] for key in
                ("policy", "revision", "max_missing_per_round", "warn_missing_per_round")}
            result["missing_by_round"] = self._missing_by_round(cursor, result, policy)
            result["held_rounds"] = [row["round"] for row in result["missing_by_round"] if row["state"] == "held"]
            result["initialized"] = True
            return result

    def _missing_by_round(self, cursor, stream, policy):
        """Unresolved missing heights of each round since the contiguous cursor, against the limits."""
        cursor.execute("SELECT round,blocks_per_round FROM observed_blocks ORDER BY height DESC LIMIT 1")
        head = cursor.fetchone()
        if not head:
            return []
        length = head["blocks_per_round"]
        cursor.execute("""SELECT expected.height/%s+1 AS round,
                count(*) FILTER (WHERE w.height IS NULL) AS missing,
                count(*) FILTER (WHERE w.height IS NOT NULL) AS waived
            FROM generate_series(%s,%s) AS expected(height)
            LEFT JOIN observed_blocks b ON b.height=expected.height
            LEFT JOIN observation_gap_waivers w ON w.height=expected.height
            WHERE b.id IS NULL GROUP BY 1 ORDER BY 1""",
            (length, stream["contiguous_height"]+1, stream["latest_seen_height"]))
        rounds = []
        for row in cursor.fetchall():
            ended = stream["latest_seen_height"] >= row["round"]*length-1
            if not ended:
                state = "open"
            elif row["missing"] > policy["max_missing_per_round"] or row["missing"] + row["waived"] >= length:
                state = "held"
            elif row["missing"] > policy["warn_missing_per_round"]:
                state = "warning"
            else:
                state = "tolerated"
            rounds.append({"round": row["round"], "missing": row["missing"], "waived": row["waived"],
                           "ended": ended, "state": state})
        return rounds

    def gap_policy(self, *, cursor=None):
        """The current missing-block tolerance: the latest append-only revision."""
        with (self.database.transaction() if cursor is None else nullcontext(cursor)) as query:
            query.execute("SELECT * FROM missing_block_policies ORDER BY revision DESC LIMIT 1")
            row = query.fetchone()
            if not row:
                raise ProtocolDataError("the missing-block tolerance policy is not recorded")
            return dict(row)

    def set_gap_policy(self, *, max_missing_per_round, warn_missing_per_round, actor, reason):
        """Append a reviewed tolerance revision; it applies to every round that has not settled."""
        for value in (max_missing_per_round, warn_missing_per_round):
            if type(value) is not int or value < 0:
                raise ValueError("missing-block limits must be nonnegative integers")
        if warn_missing_per_round > max_missing_per_round:
            raise ValueError("the warning level cannot exceed the settlement limit")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("a policy change requires an actor and reason")
        with self.database.transaction() as cursor:
            # Settlement rechecks coverage under this lock, so the limit cannot change mid-settlement.
            lock(cursor, "observation-stream")
            current = self.gap_policy(cursor=cursor)
            if (current["max_missing_per_round"], current["warn_missing_per_round"]) == (
                    max_missing_per_round, warn_missing_per_round):
                return current
            cursor.execute("""INSERT INTO missing_block_policies
                (policy,max_missing_per_round,warn_missing_per_round,actor,reason)
                VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (GAP_POLICY, max_missing_per_round, warn_missing_per_round, actor.strip(), reason.strip()))
            return dict(cursor.fetchone())

    def waive_prelaunch_gap(self, height, *, actor, reason, evidence):
        """Record the single prelaunch missing-height exception without fabricating a block.

        This is only allowed before any pool reservation exists. The settlement
        rule treats this one height as having zero pool credit; the raw gap
        remains visible in observation status and the immutable waiver record.
        """
        if type(height) is not int or height < 0:
            raise ValueError("a nonnegative missing block height is required")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("gap waiver requires an actor and reason")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("gap waiver requires nonempty evidence")
        policy = "one-time-prelaunch-zero-pool-credit-v1"
        with self.database.transaction() as cursor:
            # Match the lock order in reserve_next: observation stream, then
            # protocol-budget. This prevents a new reservation racing the waiver.
            lock(cursor, "observation-stream")
            lock(cursor, "operator:protocol-budget")
            cursor.execute("SELECT * FROM observation_gap_waivers WHERE height=%s OR policy=%s", (height, policy))
            previous = cursor.fetchone()
            if previous:
                if (previous["height"], previous["actor"], previous["reason"], previous["evidence"]) == (
                        height, actor.strip(), reason.strip(), evidence):
                    return dict(previous)
                raise ProtocolDataError("the one-time prelaunch gap waiver is already recorded")
            cursor.execute("SELECT value FROM runtime_controls WHERE name='new_work_paused'")
            paused = cursor.fetchone()
            if not paused or not paused["value"]:
                raise ProtocolDataError("prelaunch gap waiver requires new work to remain paused")
            cursor.execute("SELECT 1 FROM reservations LIMIT 1")
            if cursor.fetchone():
                raise ProtocolDataError("prelaunch gap waiver is unavailable after pool reservations exist")
            cursor.execute("SELECT * FROM observation_stream WHERE name='tig' FOR UPDATE")
            stream = cursor.fetchone()
            if not stream or height < stream["launch_height"] or height >= stream["latest_seen_height"]:
                raise ProtocolDataError("waiver height is outside the observed stream")
            cursor.execute("SELECT * FROM observed_blocks WHERE height=ANY(%s)", ([height-1, height, height+1],))
            neighbors = {row["height"]: row for row in cursor.fetchall()}
            if height in neighbors or height-1 not in neighbors or height+1 not in neighbors:
                raise ProtocolDataError("waiver requires one missing height between two captured blocks")
            previous_block, next_block = neighbors[height-1], neighbors[height+1]
            length = previous_block["blocks_per_round"]
            round_number = height // length + 1 if length else 0
            if (length <= 0 or next_block["blocks_per_round"] != length
                    or previous_block["round"] != round_number or next_block["round"] != round_number):
                raise ProtocolDataError("waiver height must be bracketed within one verified round")
            cursor.execute("SELECT height FROM observed_blocks WHERE id=%s", (next_block["previous_id"],))
            if cursor.fetchone():
                raise ProtocolDataError("the successor points to an already recorded block at another height")
            cursor.execute("""SELECT 1 FROM unresolved_observation_conflicts WHERE kind='conflicting-block'
                AND height=ANY(%s) LIMIT 1""", ([height-1, height, height+1],))
            if cursor.fetchone():
                raise ProtocolDataError("conflicting neighbor evidence prevents a gap waiver")
            cursor.execute("""INSERT INTO observation_gap_waivers
                (height,round_number,assumed_block_id,previous_observed_block_id,
                 next_observed_block_id,policy,actor,reason,evidence)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (height, round_number, next_block["previous_id"], previous_block["id"],
                 next_block["id"], policy, actor.strip(), reason.strip(), Json(evidence)))
            return dict(cursor.fetchone())

    def round_gaps(self, round_number, *, cursor=None):
        """One reward round's coverage against the missing-block policy.

        The round is complete when it has ended on chain, holds no unresolved
        conflicting block and its unresolved missing heights do not exceed the
        policy limit. Tolerated heights count as zero pool credit; they are never
        fabricated or interpolated, and a later genuine backfill still needs credit.
        """
        with (self.database.transaction() if cursor is None else nullcontext(cursor)) as query:
            policy = self.gap_policy(cursor=query)
            result = {"round": round_number, "policy": policy["policy"], "policy_revision": policy["revision"],
                      "max_missing_per_round": policy["max_missing_per_round"],
                      "warn_missing_per_round": policy["warn_missing_per_round"],
                      "blocks_per_round": None, "ended": False, "observed": 0, "credited": 0, "uncredited": 0,
                      "missing": 0, "missing_heights": [], "waived_heights": [], "conflicting": False,
                      "complete": False, "credited_complete": False, "warning": False}
            query.execute("SELECT DISTINCT blocks_per_round FROM observed_blocks WHERE round=%s", (round_number,))
            lengths = [row["blocks_per_round"] for row in query.fetchall()]
            if len(lengths) != 1:
                return result
            length = lengths[0]
            first, last = (round_number-1)*length, round_number*length-1
            query.execute("SELECT latest_seen_height FROM observation_stream WHERE name='tig'")
            stream = query.fetchone()
            query.execute("""SELECT expected.height, b.id AS block_id, w.height AS waived
                FROM generate_series(%s,%s) AS expected(height)
                LEFT JOIN observed_blocks b ON b.height=expected.height AND b.round=%s
                LEFT JOIN credited_blocks c ON c.block_id=b.id
                LEFT JOIN observation_gap_waivers w ON w.height=expected.height AND w.round_number=%s
                WHERE b.id IS NULL OR c.block_id IS NULL ORDER BY expected.height""",
                (first, last, round_number, round_number))
            rows = query.fetchall()
            missing = [row["height"] for row in rows if row["block_id"] is None and row["waived"] is None]
            waived = [row["height"] for row in rows if row["block_id"] is None and row["waived"] is not None]
            uncredited = sum(1 for row in rows if row["block_id"] is not None)
            query.execute("""SELECT 1 FROM unresolved_observation_conflicts
                WHERE kind='conflicting-block' AND height BETWEEN %s AND %s LIMIT 1""", (first, last))
            conflicting = bool(query.fetchone())
            ended = bool(stream) and stream["latest_seen_height"] >= last
            complete = ended and not conflicting and len(missing) <= policy["max_missing_per_round"]
            observed = length - len(missing) - len(waived)
            result.update(blocks_per_round=length, ended=ended, observed=observed, credited=observed-uncredited,
                          uncredited=uncredited, missing=len(missing),
                          missing_heights=missing[:max(100, policy["max_missing_per_round"])],
                          waived_heights=waived, conflicting=conflicting, complete=complete,
                          credited_complete=complete and uncredited == 0,
                          warning=len(missing) > policy["warn_missing_per_round"])
            return result

    def round_coverage(self, round_number, *, require_credits=True):
        coverage = self.round_gaps(round_number)
        return coverage["credited_complete"] if require_credits else coverage["complete"]

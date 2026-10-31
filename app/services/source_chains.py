"""Which source fingerprints are one device: decided per source, by the reconciler or the owner.

A record without a Google id is identified by its v2 key (app/parsers/identity.py), which
leaves out ``displayName`` and the package's ``.<UUID>`` suffix. Two records with one key and
two different ``dataSource`` fingerprints are either

* the same record before and after a change of its source (Google renamed the device, Apple
  Health dropped the suffix, or the old name came back): one record, one value; or
* two devices that measured the same instant: two records, as a rule with two values.

The write path never decides which (app/services/identity.py): an unknown fingerprint always
gets rows of its own, so at worst a renamed device is counted twice until the reconciler has
run. Only the reconciler (``health-mirror identity-backfill``, nightly as an incremental run)
and the owner (``health-mirror identity-decide``) decide, per data type, between fingerprints
that are *kindred* (app/parsers/identity.py ``kindred``): of one stable class (the source fields
a rename keeps), or the same ``dataSource`` but for an optional device field (``formFactor``,
``manufacturer``) - perhaps with the ``displayName`` - one has and the other lacks (Google added
``formFactor`` to a band's records). The rule runs per *group*: fingerprints connected by kindred
pairs; in data without optional device fields a group is exactly a stable class. Kindred is not
transitive (a source without ``formFactor`` is kindred to a phone and to a watch of its name), so
a link is only ever made, by the reconciler or the owner, between two devices whose fingerprints
are *compatible* two by two (``Chains.joinable``: no optional device field with two values): a
phone and a watch are never one device, through whatever forms. A fingerprint kindred to neither
form of a link says nothing about it.

**Evidence of two devices** (``raw_source_pairs``): a key that one Google response returned
under two fingerprints *with two different readings*. A renamed device never returns two values
for one record; two devices that measured one instant do. Two fingerprints are *established
distinct* when at least ``PAIR_KEYS`` (three) keys gave such evidence, in at least two fetches,
and a later fetch returned such a key for a record *newer* than the first fetch: both sources
still produce new records side by side. One response is never enough, however many keys it
holds (Google switching a name can return a few old records in both forms), nor are old records
returned again in both forms. The owner's ``distinct`` / ``not-distinct`` decision overrides the
evidence either way. Distinctness holds between *devices*: a source established distinct from
one form of a device is distinct from all its forms. Pairs established by evidence alone whose
sightings only touch (one began within ``RENAME_OVERLAP`` of the other's end: it looks like a
rename) are listed for the owner's review, and a deletion is never marked on their strength.

**The chain rule.** From every fingerprint's sighting span (``raw_source_sightings``: first and
last time a record carrying it was received, stamped with the fetch time), ``A -> B`` is a link
of one device's chain only when

1. every fingerprint of the class has a complete span (built from the whole archive by a full
   ``identity-backfill``, or first seen after that);
2. ``B`` was first seen more than ``CHAIN_GAP`` (five minutes) after ``A`` was last seen;
3. ``A`` is ``B``'s only predecessor candidate (a fingerprint last seen before ``B`` appeared
   with no other one appearing in between) and ``B`` is ``A``'s only successor candidate;
4. no other fingerprint of the class was first seen in between, or was seen alongside
   (overlapping spans) either ``A`` or ``B``;
5. ``A`` and ``B`` are not established distinct, neither already has a live link the other way
   (a chain never forks or cycles), and no link between them was rejected or refuted.

A fingerprint established distinct from ``A`` or ``B`` and with ``PAIR_KEYS`` keys of evidence
against the other (from any responses: a second watch worn alongside, its evidence against the
new name perhaps from one re-fetch of old records) is left out of rules 3 and 4: it is known to
be another device, so it says nothing about ``A -> B`` (a chain only ever merges equal
readings; deletions are never marked on that weaker evidence). A fingerprint established
distinct from ``B`` is never ``B``'s candidate; nor is one that *retired before* ``A``
(established distinct from ``A``, its newest record - ``Span.newest``, the physical time a
re-fetch of old records does not move - more than ``RETIRED_AFTER`` (seven days) older than
``A``'s and than ``B``'s first sighting): it stopped recording while ``A`` went on for days, and
its later sightings are re-fetches of old records. A device unworn for less than that (a watch
on its charger while the band records on) still competes as ``B``'s earlier form.

Everything else is *ambiguous*: nothing is linked, both keep their rows (visible; at worst
counted twice), and every run's report and ``health-mirror status`` list the case with why, for
the owner to decide (``identity-decide link`` or ``distinct``).

A link joins its two fingerprints into one *device* in both directions. It is kept for good
(``raw_source_chains`` is append-only, with the spans it was decided on as evidence). The old
form returning later does not undo it (a name can change back); its records simply get rows of
their own again, linked to the device's. A link is *live* until it is *refuted* (its two
fingerprints became established distinct: a ``refuted`` event in ``raw_source_chain_events``,
recorded by the reconciler) or the two are decided distinct by the owner, or the owner's latest
decision about it is ``reject``; a later ``not-distinct`` or ``link`` makes it live again. A
link the reconciler decided only because other live links kept a rival source out (its
*premise*, kept in its evidence: the rival continued as another source, or was a form of either
device) is live only while those are, unless the owner linked or confirmed it, so undoing those
undoes it too. Only live links make devices and must form chains (``problems`` lists any fork
or cycle); a dead link frees both its fingerprints for another link. Whenever devices change,
``record_devices`` appends the new device of each fingerprint to ``raw_source_devices``:
readers count a supersession only while its two fingerprints are one device there
(app/models/identity.py, ``in_force``), so a wrong link is undone in what they count as soon as
it is rejected or refuted. Every link the reconciler decides is listed for review until the
owner confirms or rejects it.

Concurrency: ``store_records`` and deletion marking hold a shared transaction lock per data
type, a reconciler batch the exclusive one, and only one reconciler runs at a time (a session
lock, which ``identity-decide`` shares and waits for briefly); see :func:`lock_data_type` and
:func:`backfill_lock`. Whether two fingerprints' shared keys mostly agree (``agreeing``) is
counted per key by the reconciler (``raw_source_agreements``); a refutation, and a deletion
mark on that evidence, waits until every shared key was counted (``agreement_counted``). The
reconciler asks for the exclusive lock without waiting in the lock queue (so writers never queue
behind it) and, only when that fails for ``LOCK_TIMEOUT``, waits in the queue for at most
``QUEUE_TIMEOUT``.
"""

# Compatibility API: production callers and clock overrides keep this import path.
from app.services.source_chain_evidence import _STAMP as _STAMP
from app.services.source_chain_evidence import CHAIN_GAP as CHAIN_GAP
from app.services.source_chain_evidence import MAX_CHAIN as MAX_CHAIN
from app.services.source_chain_evidence import PAIR_KEYS as PAIR_KEYS
from app.services.source_chain_evidence import RENAME_OVERLAP as RENAME_OVERLAP
from app.services.source_chain_evidence import RETIRED_AFTER as RETIRED_AFTER
from app.services.source_chain_evidence import RULE as RULE
from app.services.source_chain_evidence import Evidence as Evidence
from app.services.source_chain_evidence import Span as Span
from app.services.source_chain_evidence import back_to_back as back_to_back
from app.services.source_chain_evidence import dump_agreement as dump_agreement
from app.services.source_chain_evidence import established as established
from app.services.source_chain_evidence import evidence as evidence
from app.services.source_chain_evidence import gap_seconds as gap_seconds
from app.services.source_chain_evidence import key_time as key_time
from app.services.source_chain_evidence import ordered_pair as ordered_pair
from app.services.source_chain_evidence import overlaps as overlaps
from app.services.source_chain_evidence import owner_decision_of as owner_decision_of
from app.services.source_chain_evidence import parse_agreement as parse_agreement
from app.services.source_chain_evidence import parse_evidence as parse_evidence
from app.services.source_chain_evidence import parse_premise as parse_premise
from app.services.source_chain_evidence import precedes as precedes
from app.services.source_chain_evidence import useful as useful
from app.services.source_chain_evidence import worn_together as worn_together
from app.services.source_chain_locks import BACKFILL_LOCK as BACKFILL_LOCK
from app.services.source_chain_locks import DATA_TYPE_LOCK as DATA_TYPE_LOCK
from app.services.source_chain_locks import DEFERRAL_LOCK as DEFERRAL_LOCK
from app.services.source_chain_locks import LOCK_PURPOSES as LOCK_PURPOSES
from app.services.source_chain_locks import LOCK_TIMEOUT as LOCK_TIMEOUT
from app.services.source_chain_locks import QUEUE_TIMEOUT as QUEUE_TIMEOUT
from app.services.source_chain_locks import ROW_LOCK_TIMEOUT as ROW_LOCK_TIMEOUT
from app.services.source_chain_locks import BackfillRunning as BackfillRunning
from app.services.source_chain_locks import LockBusy as LockBusy
from app.services.source_chain_locks import _key as _key
from app.services.source_chain_locks import _lock_holder as _lock_holder
from app.services.source_chain_locks import _queue_for_lock as _queue_for_lock
from app.services.source_chain_locks import backfill_lock as backfill_lock
from app.services.source_chain_locks import lock_data_type as lock_data_type
from app.services.source_chain_locks import seconds as seconds
from app.services.source_chain_locks import try_lock_deferrals as try_lock_deferrals
from app.services.source_chain_model import Chains as Chains
from app.services.source_chain_model import SourceState as SourceState
from app.services.source_chain_repository import _span as _span
from app.services.source_chain_repository import _upsert as _upsert
from app.services.source_chain_repository import database_now as database_now
from app.services.source_chain_repository import insert_chain as insert_chain
from app.services.source_chain_repository import load_chains as load_chains
from app.services.source_chain_repository import load_state as load_state
from app.services.source_chain_repository import record_decision as record_decision
from app.services.source_chain_repository import record_devices as record_devices
from app.services.source_chain_repository import record_evidence as record_evidence
from app.services.source_chain_repository import record_refutations as record_refutations
from app.services.source_chain_repository import record_sightings as record_sightings
from app.services.source_chain_repository import write_spans as write_spans
from app.services.source_chain_rules import Decision as Decision
from app.services.source_chain_rules import decide_chains as decide_chains
from app.services.source_chain_rules import describe as describe
from app.services.source_chain_rules import kindred_groups as kindred_groups

"""Identity model imports share one ORM registry and one ordered set of archive guards."""

import importlib
import subprocess
import sys
import textwrap

import pytest

from app.models import identity


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (
            "constants",
            "IDENTITY_TABLE SUPERSESSION_TABLE CHAIN_TABLE CHAIN_EVENT_TABLE PAIR_TABLE "
            "DECISION_TABLE DEVICE_TABLE AGREEMENT_TABLE SIGHTING_TABLE RECONCILE_TABLE "
            "DEFERRAL_TABLE IDENTITY_TABLES GUARDED_TABLES DECIDERS REFUTED CHAIN_DECISIONS "
            "PAIR_DECISIONS DECISIONS REASONS MERGED_EVENT",
        ),
        (
            "tables",
            "RawHealthDataIdentity RawHealthDataSupersession RawSourceChain RawSourceChainEvent "
            "RawSourceDevice RawSourcePair RawSourceDecision IdentityReconcileState "
            "RawSourceAgreement IdentityDeferredDeletion RawSourceSighting",
        ),
        (
            "queries",
            "joined_pairs in_force in_force_sql superseded held_elsewhere plan_each_query "
            "key_held_elsewhere holds_live",
        ),
        (
            "guards",
            "SIGHTINGS_FUNCTION FUNCTION_BODIES PROTECTIVE_TRIGGERS PG_TRIGGER_SQL "
            "SQLITE_TRIGGER_SQL POSTGRESQL_DDL SQLITE_DDL",
        ),
    ],
)
def test_legacy_model_exports_are_the_same_objects(module, names):
    implementation = importlib.import_module(f"app.models.identity_{module}")
    for name in names.split():
        exported = getattr(identity, name)
        assert exported is getattr(implementation, name)
        if callable(exported):
            assert exported.__module__ == implementation.__name__


@pytest.mark.parametrize(
    "first",
    [
        "identity",
        "identity_constants",
        "identity_tables",
        "identity_queries",
        "identity_guards",
        "raw_versions",
        "manual_flags",
        "retrieval",
        "webhook_status",
    ],
)
def test_cold_imports_register_models_and_guards_once_in_archive_order(first):
    # Use a fresh interpreter: conftest already imports app.models in the test process.
    script = textwrap.dedent(
        """
        import importlib
        import sys
        from collections import Counter
        from sqlalchemy import create_engine, create_mock_engine, text

        importlib.import_module(sys.argv[1])
        from app.db.base import Base
        from app.models import identity, identity_constants, identity_guards, identity_tables
        from app.models import derived_invalidation, manual_flags, raw_versions, retrieval
        from app.models import webhook_status

        assert 'app.services.identity' not in sys.modules
        assert 'app.services.raw_history' not in sys.modules
        listeners = tuple(Base.metadata.dispatch.after_create)
        mappers = set(Base.registry.mappers)
        tables = dict(Base.metadata.tables)
        for name in ('identity_queries', 'identity_tables', 'identity_constants',
                     'identity_guards', 'raw_versions', 'manual_flags', 'retrieval',
                     'webhook_status', 'identity'):
            importlib.import_module('app.models.' + name)
        importlib.reload(identity)
        assert tuple(Base.metadata.dispatch.after_create) == listeners
        assert set(Base.registry.mappers) == mappers
        assert dict(Base.metadata.tables) == tables

        names = (*identity_constants.GUARDED_TABLES, identity_constants.RECONCILE_TABLE,
                 identity_constants.DEFERRAL_TABLE)
        assert len(names) == 11
        for name in names:
            mapped = [mapper for mapper in mappers if mapper.local_table.name == name]
            assert len(mapped) == 1, name
            cls = mapped[0].class_
            assert cls is getattr(identity, cls.__name__)
            assert cls is getattr(identity_tables, cls.__name__)
            assert cls.__table__ is Base.metadata.tables[name]
        order = list(tables)
        assert order.index('raw_health_data_versions') < order.index(identity.IDENTITY_TABLE)

        for dialect, ddl in (('postgresql', 'POSTGRESQL_DDL'), ('sqlite', 'SQLITE_DDL')):
            statements = []
            def capture(sql, *args, **kwargs):
                statements.append(str(sql.compile(dialect=engine.dialect)))
            engine = create_mock_engine(dialect + '://', capture)
            Base.metadata.create_all(engine, checkfirst=False)
            expected = (
                *getattr(raw_versions, ddl),
                *getattr(identity_guards, ddl),
                *getattr(manual_flags, ddl),
                *getattr(retrieval, ddl),
                *getattr(webhook_status, ddl),
                *getattr(derived_invalidation, ddl),
            )
            assert [sql for sql in statements if sql in expected] == list(expected)
            counts = Counter(statements)
            for sql in expected:
                assert counts[sql] == 1, sql

        # Execute SQLite DDL too: duplicate trigger registration must fail create_all.
        engine = create_engine('sqlite://')
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            triggers = dict(connection.execute(text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )).all())
        expected = (
            raw_versions.SQLITE_TRIGGER_SQL
            | identity_guards.SQLITE_TRIGGER_SQL
            | manual_flags.SQLITE_TRIGGER_SQL
            | retrieval.SQLITE_TRIGGER_SQL
            | webhook_status.SQLITE_TRIGGER_SQL
            | derived_invalidation.SQLITE_TRIGGER_SQL
        )
        assert triggers.keys() == expected.keys()
        for name, (_, sql) in expected.items():
            assert triggers[name] == sql
        engine.dispose()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, f"app.models.{first}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

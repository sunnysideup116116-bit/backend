"""Universal preference identity includes recovery and historical CLI boundaries."""
from copy import deepcopy
from unittest.mock import Mock
import pytest

from matchmaker_agent import preference_bootstrap as bootstrap
from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.preference_bootstrap_contract import BootstrapError
from scripts import migrate_neo4j_preferences as historical


@pytest.mark.parametrize('relation', ['PREFERS', 'AVOIDS'])
@pytest.mark.parametrize('method', ['check_rollback', 'rollback'])
def test_legacy_rollback_fails_before_any_association_or_journal_write(monkeypatch, relation, method):
    current = {'revision': 1, 'rows': []}
    record = {'status': 'committed', 'revision_after': 1, 'after': current,
              'created_edges': [{'key': 'must-not-delete'}],
              'retired_edges': [{'relation': relation, 'concept': {'key': 'legacy', 'label': 'Reading'}}]}
    before = deepcopy(record)
    tx = Mock(); tx.run.side_effect = AssertionError('No graph mutation/query after legacy journal rejection')
    monkeypatch.setattr(bootstrap, 'journal', lambda *_: deepcopy(record))
    monkeypatch.setattr(bootstrap, 'snapshot', lambda *_: (deepcopy(current), None))
    monkeypatch.setattr(bootstrap, 'lock_preferences', Mock())
    save = Mock(); monkeypatch.setattr(bootstrap, 'save_journal', save)
    graph = bootstrap.BootstrapGraph(None)
    graph.read = graph.write = lambda callback: callback(tx)
    with pytest.raises(BootstrapError, match='rollback_legacy_restore_forbidden') as exc:
        getattr(graph, method)('owner', 'operation', 1)
    assert exc.value.status == 409 and record == before
    tx.run.assert_not_called(); save.assert_not_called()


def test_v2_only_rollback_authority_is_preserved_and_locked():
    identity = canonicalize_concept('Reading').as_dict()
    record = {'retired_edges': [{'relation': 'PREFERS', 'concept': identity}]}
    tx = Mock(); tx.run.return_value = [{'concept': deepcopy(identity)}]
    bootstrap.require_v2_rollback(tx, record, lock=True)
    assert 'SET c.key=c.key' in tx.run.call_args.args[0]
    assert tx.run.call_args.kwargs['key'] == identity['key']
    bootstrap.require_v2_rollback(tx, {'retired_edges': []})


def test_v2_archive_with_changed_actual_target_fails_closed():
    identity = canonicalize_concept('Reading').as_dict()
    tx = Mock(); tx.run.return_value = [{'concept': {**identity, 'semantic_input_hash': 'tampered'}}]
    with pytest.raises(BootstrapError, match='rollback_identity_changed'):
        bootstrap.require_v2_rollback(tx, {'retired_edges': [{'relation': 'AVOIDS', 'concept': identity}]}, lock=True)


def test_historical_apply_rejected_before_credentials_connections_or_mutation(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['migrate_neo4j_preferences.py', '--apply'])
    mongo = Mock(side_effect=AssertionError('No Mongo connection'))
    graph = Mock(side_effect=AssertionError('No Graph connection'))
    env = Mock(side_effect=AssertionError('No secret loading'))
    monkeypatch.setattr(historical, 'MongoClient', mongo)
    monkeypatch.setattr(historical.GraphDatabase, 'driver', graph)
    monkeypatch.setattr(historical, 'load_dotenv', env)
    with pytest.raises(SystemExit) as exc:
        historical.main()
    assert exc.value.code == 2
    assert 'legacy_preference_apply_forbidden' in capsys.readouterr().err
    mongo.assert_not_called(); graph.assert_not_called(); env.assert_not_called()

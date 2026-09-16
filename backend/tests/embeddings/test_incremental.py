from unittest.mock import Mock

import pytest

from app.embeddings.schema import IndexRecord
from tests.embeddings.test_indexer import settings, encoder, index  # noqa: F401


def record(text='Evidence', year=2024):
    return IndexRecord('12345678-1234-1234-1234-123456789012', 1, text, 'paragraph', None, year, 'ACL', 'Results', 'node')


def test_unchanged_vectors_reused_and_metadata_updated(index):
    index.sync_paper(1, [record()])
    original = index.encoder.encode
    index.encoder.encode = Mock(side_effect=AssertionError('must not re-embed'))
    assert index.sync_paper(1, [record(year=2025)]) == 0
    page, _ = index.client.scroll(index.collection, with_payload=True)
    assert page[0].payload['year'] == 2025
    assert page[0].payload['text_sha256']
    assert page[0].payload['embed_version']
    assert index.paper_current(1, [record(year=2025)])
    index.encoder.encode = original
    assert index.sync_paper(1, [record(text='Changed evidence')]) == 1
    assert index.sync_paper(1, []) == 0
    assert not index.has_evidence()


def test_prune_failure_is_not_hidden(index):
    index.sync_paper(1, [record()])
    index.client.delete = Mock(side_effect=RuntimeError('failed'))
    with pytest.raises(RuntimeError):
        index.sync_paper(1, [record()])


def test_model_revision_invalidates_embedding(index):
    index.sync_paper(1, [record()])
    index.settings.embedding_model_revision = 'new-revision'
    assert not index.paper_current(1, [record()])
    assert index.sync_paper(1, [record()]) == 1


def test_length_sorted_batches_keep_vectors_attached_to_their_record(index):
    from dataclasses import replace
    index.settings.embedding_sort_by_length = True
    records = [replace(record(text='x' * n), record_id=f'12345678-1234-1234-1234-{i:012d}') for i, n in enumerate([100, 1, 50])]
    calls = []
    def encode(texts, batch_size):
        calls.extend(texts)
        return [[float(len(t)), 1., 0., 0.] for t in texts]
    # Use the fixture encoder dimension, preserving its collection contract.
    index.encoder.dimension = 4
    index.encoder.encode = encode
    index.upsert(records)
    assert [len(t) for t in calls] == [1, 50, 100]
    points, _ = index.client.scroll(index.collection, with_vectors=True)
    for point in points:
        assert point.vector[0] / point.vector[1] == pytest.approx(len(point.payload['text']), rel=1e-5)

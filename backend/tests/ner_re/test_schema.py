"""Tests for app/ner_re/schema.py — the Phase 4 joint-extraction contract.

`app/ner_re/graph.py` and `tests/ner_re/test_graph.py` already depend on this
module's exact shape, so these tests pin it rather than invent it:

* `EntityMention(text, entity_type, confidence)` — positional
* `RelationMention(head, relation_type, tail, confidence)` — positional, with
  head/tail carrying entity **text** (graph.py calls `.casefold()` on them)
* `RELATION_ENDPOINTS[relation] -> (head_type, tail_type)` — exactly a 2-tuple,
  because graph.py unpacks it into two names
"""

import pytest

from app.ner_re.schema import (
    ENTITY_TYPES,
    RELATION_ENDPOINTS,
    EntityMention,
    JointExtractionResult,
    RelationMention,
)


def test_the_five_readme_entity_types_are_declared() -> None:
    assert ENTITY_TYPES == frozenset({"model", "dataset", "task", "metric", "domain"})


def test_the_four_readme_relations_are_declared() -> None:
    assert set(RELATION_ENDPOINTS) == {
        "trained_on",
        "evaluated_using",
        "fails_on",
        "achieves",
    }


def test_relation_endpoints_match_the_readme_worked_examples() -> None:
    """README §8 Stage 4: BERT-trained_on->SQuAD, GPT-4-evaluated_using->GSM8K,
    BERT-fails_on->Low-resource Languages; Stage 3: Contribution -> SOTA accuracy."""
    assert RELATION_ENDPOINTS["trained_on"] == ("model", "dataset")
    assert RELATION_ENDPOINTS["evaluated_using"] == ("model", "dataset")
    assert RELATION_ENDPOINTS["fails_on"] == ("model", "domain")
    assert RELATION_ENDPOINTS["achieves"] == ("model", "metric")


def test_every_endpoint_is_a_two_tuple_of_known_entity_types() -> None:
    """graph.py does `head_type, tail_type = RELATION_ENDPOINTS[...]`."""
    for relation, endpoints in RELATION_ENDPOINTS.items():
        assert isinstance(endpoints, tuple), relation
        assert len(endpoints) == 2, relation
        assert set(endpoints) <= ENTITY_TYPES, relation


def test_relation_names_survive_graph_py_label_derivation() -> None:
    """graph.py builds `RELATION_LABELS = {name: name.upper() ...}` and
    interpolates the result straight into Cypher, so a name carrying anything
    outside [A-Z_] would be a query-injection surface."""
    for relation in RELATION_ENDPOINTS:
        assert relation.replace("_", "").isalnum()
        assert relation.islower()
        assert relation.upper().replace("_", "").isalpha()


# --------------------------------------------------------------------------
# EntityMention
# --------------------------------------------------------------------------


def test_entity_mention_is_positional_text_type_confidence() -> None:
    entity = EntityMention("BERT", "model", 0.9)

    assert entity.text == "BERT"
    assert entity.entity_type == "model"
    assert entity.confidence == 0.9


def test_entity_mention_rejects_an_unknown_entity_type() -> None:
    with pytest.raises(ValueError):
        EntityMention("BERT", "algorithm", 0.9)


def test_entity_mention_rejects_blank_text() -> None:
    with pytest.raises(ValueError):
        EntityMention("   ", "model", 0.9)


# --------------------------------------------------------------------------
# RelationMention
# --------------------------------------------------------------------------


def test_relation_mention_is_positional_head_relation_tail_confidence() -> None:
    relation = RelationMention("BERT", "trained_on", "SQuAD", 0.7)

    assert relation.head == "BERT"
    assert relation.relation_type == "trained_on"
    assert relation.tail == "SQuAD"
    assert relation.confidence == 0.7


def test_relation_mention_rejects_an_unknown_relation_type() -> None:
    with pytest.raises(ValueError):
        RelationMention("BERT", "invented_by", "SQuAD", 0.7)


def test_relation_mention_rejects_blank_endpoints() -> None:
    with pytest.raises(ValueError):
        RelationMention("", "trained_on", "SQuAD", 0.7)
    with pytest.raises(ValueError):
        RelationMention("BERT", "trained_on", "", 0.7)


# --------------------------------------------------------------------------
# JointExtractionResult
# --------------------------------------------------------------------------


def test_joint_extraction_result_holds_entities_and_relations() -> None:
    result = JointExtractionResult(
        entities=[EntityMention("BERT", "model", 0.9)],
        relations=[RelationMention("BERT", "trained_on", "SQuAD", 0.7)],
    )

    assert result.entities[0].text == "BERT"
    assert result.relations[0].relation_type == "trained_on"


def test_joint_extraction_result_defaults_to_empty() -> None:
    result = JointExtractionResult()

    assert result.entities == []
    assert result.relations == []


def test_two_empty_results_do_not_share_mutable_state() -> None:
    first = JointExtractionResult()
    second = JointExtractionResult()

    first.entities.append(EntityMention("BERT", "model", 0.9))

    assert second.entities == []

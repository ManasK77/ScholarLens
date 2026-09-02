from app.clustering.analysis import labels_with_singletons, pair_indexes


def test_noise_points_are_preserved_as_singleton_clusters():
    assert labels_with_singletons([0, 0, -1, -1]) == [0, 0, 1, 2]


def test_pair_indexes_generates_each_pair_once():
    assert pair_indexes(4) == [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]

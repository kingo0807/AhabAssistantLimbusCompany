from tasks.mirror.search_road import Row, RouteGraph, all_node_weight


def test_route_weights_are_time_oriented_and_preserve_battle_severity() -> None:
    assert all_node_weight["event"] == 1
    assert all_node_weight["shop"] == 2
    assert all_node_weight["battle"] == 6
    assert all_node_weight["focused_encounter"] == 8
    assert all_node_weight["abnormality_focused_encounter"] == 8
    assert all_node_weight["risky_encounter"] == 9


def test_connections_are_applied_to_their_actual_later_columns() -> None:
    graph = RouteGraph(
        [
            [["event", (300, 560)]],
            [["event", (600, 560)]],
            [["boss_battle", (900, 300)]],
        ],
        bus_row=Row.MID,
        bus_position=(100, 560),
        hard_mode=False,
    )

    graph.init_road(
        [
            (1, Row.MID, Row.MID),
            (2, Row.MID, Row.MID),
            (3, Row.MID, Row.TOP),
        ]
    )

    weight, path = graph.find_min_weight_route()

    assert weight != float("inf")
    assert [node.node_class for node in path] == ["bus", "event", "event", "boss_battle"]


def test_internal_boss_misclassification_is_not_used_as_terminal() -> None:
    graph = RouteGraph(
        [
            [["boss_battle", (300, 560)]],
            [["battle", (600, 560)]],
            [["boss_battle", (900, 560)]],
        ],
        bus_row=Row.MID,
        bus_position=(100, 560),
        hard_mode=False,
    )
    graph.init_road(
        [
            (1, Row.MID, Row.MID),
            (2, Row.MID, Row.MID),
            (3, Row.MID, Row.MID),
        ]
    )

    _, path = graph.find_min_weight_route()

    assert [node.node_class for node in path] == ["bus", "boss_battle", "battle", "boss_battle"]

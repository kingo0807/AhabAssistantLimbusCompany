from tasks.mirror.search_road import Node, RouteGraph, Row, all_node_weight


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


def test_runtime_route_graph_prefers_fewer_battles_when_weight_is_equal() -> None:
    graph = object.__new__(RouteGraph)
    graph.bus_row = Row.MID
    graph.column_count = 4

    start = Node("bus", 1)
    battle = Node("battle", 3)
    safe_event = Node("event", 2)
    event_after_battle = Node("event", 3)
    safe_shop = Node("shop", 4)
    boss = Node("boss_battle", 1)
    start.add_next_node(battle)
    start.add_next_node(safe_event)
    battle.add_next_node(event_after_battle)
    safe_event.add_next_node(safe_shop)
    event_after_battle.add_next_node(boss)
    safe_shop.add_next_node(boss)

    graph.columns = {
        "column1": {Row.MID: start},
        "column2": {Row.TOP: battle, Row.BOTTOM: safe_event},
        "column3": {Row.TOP: event_after_battle, Row.BOTTOM: safe_shop},
        "column4": {Row.MID: boss},
    }

    weight, path = graph.find_min_weight_route()

    assert weight == 8
    assert path == [start, safe_event, safe_shop, boss]

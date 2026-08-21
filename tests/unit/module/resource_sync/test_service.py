from module.resource_sync.manifest import ResourceManifest
from module.resource_sync.service import PROTECTED_LOCAL_IMAGE_PATHS, ResourceSyncService


def test_route_templates_removed_from_remote_manifest_remain_protected():
    assert {
        "default/share/mirror/road_in_mir/up.png",
        "default/share/mirror/road_in_mir/mid.png",
        "default/share/mirror/road_in_mir/down.png",
    }.issubset(PROTECTED_LOCAL_IMAGE_PATHS)


def test_sync_plan_keeps_images_still_referenced_by_program(tmp_path):
    assets_dir = tmp_path / "images"
    for relative_path in PROTECTED_LOCAL_IMAGE_PATHS:
        path = assets_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"compatibility template")

    obsolete = assets_dir / "default/share/obsolete.png"
    obsolete.parent.mkdir(parents=True, exist_ok=True)
    obsolete.write_bytes(b"obsolete")

    service = ResourceSyncService(
        assets_dir=assets_dir,
        state_path=tmp_path / "state.json",
        temp_dir=tmp_path / "update_temp",
        sources={},
    )
    plan = service.build_sync_plan(ResourceManifest())

    assert plan.files_to_delete == ["default/share/obsolete.png"]
    assert PROTECTED_LOCAL_IMAGE_PATHS.isdisjoint(plan.files_to_delete)

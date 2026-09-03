"""计划导入的端点与落库。

解析本身由 test_plan_import.py 覆盖（纯函数，脏数据的各种形态）。这里管的是
另一半：上传怎么拒绝、什么该落库什么不该、以及导入完助理能不能读到。
"""

import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook

from app.main import app
from app.services.plan_import import IMPORTABLE_PROFILE_FIELDS

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def _plan_bytes(weeks: int = 2, with_basics: bool = True) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    if with_basics:
        sheet = workbook.create_sheet("基础信息")
        for row in (
            ["当前体重(kg)", 91], ["目标体重(kg)", 86], ["身高(cm)", 180],
            ["年龄(岁)", 25], ["性别", "男"], ["每日总消耗(kcal)", 2415],
        ):
            sheet.append(row)
    for index in range(1, weeks + 1):
        sheet = workbook.create_sheet(f"第{index}周")
        sheet.append([f"第{index}周", *WEEKDAYS])
        sheet.append(["碳日", "中", "低", "高", "低", "中", "中", "高"])
        sheet.append(["训练部位", "胸+三头", "背+二头", "腿+肩", "休息", "胸", "背", "腿"])
        sheet.append(["碳水(g)", 140, 90, 301, 90, 140, 140, 301])
        sheet.append(["蛋白质(g)", *([112] * 7)])
        sheet.append(["脂肪(g)", 56, 121, 36, 121, 56, 56, 36])
        sheet.append(["热量(kcal)", 1512, 1897, 1976, 1897, 1512, 1512, 1976])
        sheet.append(["缺口/盈余(kcal)", -903, -518, -439, -518, -903, -903, -439])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _upload(data: bytes, name: str = "plan.xlsx") -> dict:
    return {"file": (name, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as instance:
        yield instance


@pytest.fixture
async def auth(client) -> dict:
    register = await client.post("/api/v1/auth/register", json={
        "email": f"import-{uuid.uuid4().hex[:8]}@t.com", "password": "pw123456",
    })
    return {"Authorization": f"Bearer {register.json()['access_token']}"}


class TestUploadGuards:
    @pytest.mark.asyncio
    async def test_requires_auth(self, client):
        response = await client.post("/api/v1/plans/import/preview", files=_upload(_plan_bytes()))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_non_xlsx_with_actionable_message(self, client, auth):
        """告诉用户该怎么办，而不是只说"格式不对"。"""
        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth,
            files=_upload(b"col1,col2\n1,2", name="plan.csv"),
        )
        assert response.status_code == 400
        assert "另存为" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_empty_file(self, client, auth):
        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(b""),
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_oversized_file(self, client, auth):
        """openpyxl 在内存里展开表格，几十 MB 能把 worker 拖垮。
        拒绝的代价只是一句提示。"""
        from app.api.v1.plans import MAX_UPLOAD_BYTES

        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth,
            files=_upload(b"x" * (MAX_UPLOAD_BYTES + 1)),
        )
        assert response.status_code == 413

    @pytest.mark.asyncio
    async def test_unparseable_xlsx_is_400_not_500(self, client, auth):
        """文件问题是用户的问题，不是服务端故障。500 会让这类失败混进
        真正的故障告警里。"""
        workbook = Workbook()
        workbook.active.append(["姓名", "张三"])
        buffer = io.BytesIO()
        workbook.save(buffer)

        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(buffer.getvalue()),
        )
        assert response.status_code == 400
        assert "周计划" in response.json()["detail"]


class TestPreviewDoesNotWrite:
    """预览必须是只读的。它存在的全部理由就是让用户在数据进库前看一眼。"""

    @pytest.mark.asyncio
    async def test_preview_returns_structure(self, client, auth):
        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(_plan_bytes()),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["week_count"] == 2
        assert body["day_count"] == 14
        assert body["carb_cycle"] is True
        assert body["fingerprint"]

    @pytest.mark.asyncio
    async def test_preview_creates_no_plan(self, client, auth):
        await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(_plan_bytes()),
        )
        plans = (await client.get("/api/v1/plans", headers=auth)).json()
        assert plans == []

    @pytest.mark.asyncio
    async def test_preview_does_not_touch_profile(self, client, auth):
        await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(_plan_bytes()),
        )
        profile = await client.get("/api/v1/profile", headers=auth)
        assert profile.json() == {}

    @pytest.mark.asyncio
    async def test_profile_changes_are_listed_for_review(self, client, auth):
        """逐项列出并带中文标签，用户才能看清这次会动档案里的哪几项。"""
        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(_plan_bytes()),
        )
        updates = {item["field"]: item for item in response.json()["profile_updates"]}
        assert set(updates) == set(IMPORTABLE_PROFILE_FIELDS)
        assert updates["weight_kg"]["value"] == 91.0
        assert "体重" in updates["weight_kg"]["label"]

    @pytest.mark.asyncio
    async def test_goal_fields_are_never_importable(self, client, auth):
        """target_kg 是"用户想要什么"，不该由一份下载来的模板决定——
        模板作者的目标体重与用户无关。"""
        response = await client.post(
            "/api/v1/plans/import/preview", headers=auth, files=_upload(_plan_bytes()),
        )
        fields = {item["field"] for item in response.json()["profile_updates"]}
        assert "target_kg" not in fields
        assert "goal" not in fields


class TestCommit:
    @pytest.mark.asyncio
    async def test_creates_plan(self, client, auth):
        response = await client.post(
            "/api/v1/plans/import", headers=auth, files=_upload(_plan_bytes()),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["created"] is True
        assert body["week_count"] == 2

        plans = (await client.get("/api/v1/plans", headers=auth)).json()
        assert len(plans) == 1
        assert plans[0]["id"] == body["plan_id"]
        assert plans[0]["source"] == "import"
        assert plans[0]["week_count"] == 2
        assert plans[0]["day_count"] == 14

    @pytest.mark.asyncio
    async def test_profile_untouched_by_default(self, client, auth):
        """档案是用户身份的一部分，默认不动。这个默认值的方向比它省下的
        一次点击重要得多——网上流传的模板都带着原作者的身体数据。"""
        response = await client.post(
            "/api/v1/plans/import", headers=auth, files=_upload(_plan_bytes()),
        )
        assert response.json()["profile_applied"] == {}
        profile = await client.get("/api/v1/profile", headers=auth)
        assert profile.json() == {}

    @pytest.mark.asyncio
    async def test_profile_applied_when_opted_in(self, client, auth):
        response = await client.post(
            "/api/v1/plans/import", headers=auth,
            files=_upload(_plan_bytes()), data={"apply_profile": "true"},
        )
        assert response.json()["profile_applied"]["weight_kg"] == 91.0

        profile = (await client.get("/api/v1/profile", headers=auth)).json()
        assert profile["weight_kg"] == 91.0
        assert profile["height_cm"] == 180.0
        assert profile["age"] == 25
        assert profile["sex"] == "male"
        assert "target_kg" not in profile, "目标体重被导入了"

    @pytest.mark.asyncio
    async def test_weight_import_also_records_body_metric(self, client, auth):
        """复用 merge_profile 而不是自己写 profile 表，顺带得到体重联动。"""
        await client.post(
            "/api/v1/plans/import", headers=auth,
            files=_upload(_plan_bytes()), data={"apply_profile": "true"},
        )
        metrics = (await client.get("/api/v1/logs/body-metrics", headers=auth)).json()
        assert [m["weight_kg"] for m in metrics] == [91.0]

    @pytest.mark.asyncio
    async def test_source_filename_is_kept(self, client, auth):
        """过几周回来看计划列表，文件名比一串 UUID 有用得多。"""
        await client.post(
            "/api/v1/plans/import", headers=auth,
            files=_upload(_plan_bytes(), name="凯圣王碳循环计划_91kg.xlsx"),
        )
        plans = (await client.get("/api/v1/plans", headers=auth)).json()
        assert plans[0]["source_name"] == "凯圣王碳循环计划_91kg.xlsx"


    @pytest.mark.asyncio
    async def test_list_omits_full_week_data(self, client, auth):
        """列表接口只回摘要。一份 8 周计划展开有 56 天 × 6 个字段，列表页用不上，
        而这个接口会被前端在每次导入后刷新。"""
        await client.post(
            "/api/v1/plans/import", headers=auth, files=_upload(_plan_bytes(weeks=4)),
        )
        row = (await client.get("/api/v1/plans", headers=auth)).json()[0]
        assert "weeks" not in row
        assert row["week_count"] == 4
        assert row["day_count"] == 28


class TestIdempotency:
    """用户点两次上传、或者网络重试，不该在计划列表里堆出两份一样的东西。"""

    @pytest.mark.asyncio
    async def test_same_file_twice_creates_one_plan(self, client, auth):
        data = _plan_bytes()
        first = await client.post("/api/v1/plans/import", headers=auth, files=_upload(data))
        second = await client.post("/api/v1/plans/import", headers=auth, files=_upload(data))

        assert first.json()["created"] is True
        assert second.json()["created"] is False
        assert second.json()["plan_id"] == first.json()["plan_id"]
        assert len((await client.get("/api/v1/plans", headers=auth)).json()) == 1

    @pytest.mark.asyncio
    async def test_fingerprint_ignores_filename(self, client, auth):
        """按内容算指纹而不是文件字节：同一份计划另存一次字节就变了，
        而内容一模一样。用户不会理解为什么又多了一份。"""
        data = _plan_bytes()
        await client.post("/api/v1/plans/import", headers=auth, files=_upload(data, "a.xlsx"))
        second = await client.post(
            "/api/v1/plans/import", headers=auth, files=_upload(data, "b.xlsx"),
        )
        assert second.json()["created"] is False
        assert len((await client.get("/api/v1/plans", headers=auth)).json()) == 1

    @pytest.mark.asyncio
    async def test_different_plan_creates_second_record(self, client, auth):
        await client.post(
            "/api/v1/plans/import", headers=auth, files=_upload(_plan_bytes(weeks=2)),
        )
        response = await client.post(
            "/api/v1/plans/import", headers=auth, files=_upload(_plan_bytes(weeks=4)),
        )
        assert response.json()["created"] is True
        assert len((await client.get("/api/v1/plans", headers=auth)).json()) == 2


class TestIsolation:
    @pytest.mark.asyncio
    async def test_other_user_cannot_see_imported_plan(self, client, auth):
        data = _plan_bytes()
        mine = await client.post("/api/v1/plans/import", headers=auth, files=_upload(data))

        other = await client.post("/api/v1/auth/register", json={
            "email": f"other-{uuid.uuid4().hex[:8]}@t.com", "password": "pw123456",
        })
        other_auth = {"Authorization": f"Bearer {other.json()['access_token']}"}

        # 同一份文件对另一个用户来说是全新的：指纹相同但 user_id 不同，
        # 必须新建而不是复用别人那条。
        theirs = await client.post(
            "/api/v1/plans/import", headers=other_auth, files=_upload(data),
        )
        assert theirs.json()["created"] is True
        assert theirs.json()["plan_id"] != mine.json()["plan_id"]

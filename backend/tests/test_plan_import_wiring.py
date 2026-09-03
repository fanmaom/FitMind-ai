"""计划导入的前端接线。

前端没有测试框架（package.json 里只有 dev/build/start/gen），项目一贯做法是
在后端用读源码的断言守住接线——后端能导入但界面上没有入口，等于没做。
"""

import inspect
import re
from pathlib import Path

WEB_SRC = Path(__file__).resolve().parents[2] / "web" / "src"


def _read(relative: str) -> str:
    return (WEB_SRC / relative).read_text()


class TestUploadPath:
    def test_upload_does_not_force_json_content_type(self):
        """multipart 的边界串必须由浏览器根据 FormData 自己生成。手动设
        Content-Type 会让服务端解析不出任何字段，报的还是"缺少必填字段 file"
        这种指向完全错误的信息。"""
        source = _read("services/api.ts")
        assert "export async function upload" in source
        upload_body = source.split("export async function upload")[1].split("\n}")[0]
        assert "Content-Type" not in upload_body

    def test_errors_are_rendered_as_sentences(self):
        """原来直接用 response.text()，于是 {"detail":"邮箱已注册"} 被原样
        显示成那串 JSON——用户看到的是花括号和引号。"""
        source = _read("services/api.ts")
        assert "readError" in source
        assert "parseApiError(JSON.parse(raw)" in source


class TestDialogWiring:
    def test_dialog_exists_and_is_mounted(self):
        assert "PlanImportDialog" in _read("components/chat/ChatView.tsx")
        assert (WEB_SRC / "components/plans/PlanImportDialog.tsx").exists()

    def test_entry_point_is_visible_in_header(self):
        """功能藏起来等于没做。"""
        source = _read("components/chat/ChatView.tsx")
        assert "导入计划表" in source

    def test_empty_state_mentions_import(self):
        """新用户第一眼要知道"我手上那份表可以直接传进来"。"""
        assert "导入计划表" in _read("components/chat/ChatView.tsx")

    def test_preview_before_commit(self):
        """预览是数据进库前唯一能发现解析错位的机会。"""
        source = _read("components/plans/PlanImportDialog.tsx")
        assert "/plans/import/preview" in source
        assert "/plans/import" in source

    def test_full_table_is_shown_for_review(self):
        """把整张表摆出来核对，而不是只报"共 28 天"。"""
        source = _read("components/plans/PlanImportDialog.tsx")
        assert "<table" in source
        assert "day.carb_g" in source
        assert "day.focus" in source

    def test_profile_checkbox_defaults_to_unchecked(self):
        """网上流传的模板都带着原作者的身体数据，直接写进去等于把别人的体重
        当成自己的——而档案每轮都注入 prompt，之后所有热量计算都会算错。"""
        source = _read("components/plans/PlanImportDialog.tsx")
        assert "useState(false)" in source
        assert "applyProfile" in source
        assert "确认是你自己的再勾选" in source

    def test_warnings_are_surfaced(self):
        """解析器的告警必须给用户看到，否则"已忽略某个越界值"这件事就悄悄
        发生了。"""
        assert "preview.warnings" in _read("components/plans/PlanImportDialog.tsx")

    def test_duplicate_import_is_explained(self):
        """幂等命中时要说清"没有重复添加"，否则用户会以为导入失败了。"""
        source = _read("components/plans/PlanImportDialog.tsx")
        assert "result.created" in source
        assert "已经导入过" in source

    def test_success_state_tells_what_to_do_next(self):
        """导入完成不是终点——要引导用户去用它。"""
        assert "今天吃多少" in _read("components/plans/PlanImportDialog.tsx")


class TestBackendContract:
    def test_list_endpoint_returns_summary_only(self):
        """一份 8 周计划展开有 56 天 × 6 个字段，列表页用不上，而这个接口会被
        前端在每次导入后刷新。

        断言写在返回的字面量上，而不是搜源码有没有 "weeks" 字样——那个词也
        出现在读 payload 算计数的地方，搜字符串会误报。
        """
        import app.api.v1.plans as plans_api

        source = inspect.getsource(plans_api.list_plans)
        # 返回体的键都是这个形式：`"week_count": ...`
        returned_keys = set(re.findall(r'"(\w+)":', source))
        assert {"week_count", "day_count", "source_name"} <= returned_keys
        assert "weeks" not in returned_keys, "列表接口回了完整的 weeks，会拖慢每次刷新"

    def test_profile_import_is_opt_in_at_api_level(self):
        """默认值的方向比它省下的一次点击重要得多——不能只靠前端不勾。"""
        import app.api.v1.plans as plans_api

        source = inspect.getsource(plans_api.commit_plan_import)
        assert "default=False" in source

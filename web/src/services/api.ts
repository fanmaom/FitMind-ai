const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000/api/v1";
const TOKEN_KEY = "fitness-agent-token";

export const getToken = () => typeof window === "undefined" ? "" : localStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}), ...init.headers },
  });
  if (!response.ok) throw new Error(await readError(response, `请求失败 ${response.status}`));
  if (response.status === 204) return undefined as T;
  return response.json();
}

/** 上传文件。
 *
 * 不能走 api()：那里写死了 Content-Type: application/json，而 multipart 的
 * 边界串（boundary）必须由浏览器根据 FormData 自己生成。手动设这个头会让
 * 服务端解析不出任何字段，报的还是"缺少必填字段 file"这种指向完全错误的信息。
 */
export async function upload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
    body: form,
  });
  if (!response.ok) throw new Error(await readError(response, "上传失败"));
  return response.json();
}

/** 把错误响应读成一句人话。
 *
 * 原来直接用 response.text()，于是 FastAPI 的 {"detail":"邮箱已注册"} 会被
 * 原样显示成那串 JSON。用户看到的是花括号和引号，而不是"邮箱已注册"。
 */
async function readError(response: Response, fallback: string): Promise<string> {
  const raw = await response.text().catch(() => "");
  if (!raw) return fallback;
  try {
    return parseApiError(JSON.parse(raw), fallback);
  } catch {
    // 不是 JSON（网关的 HTML 错误页之类），原样返回但别把整页塞给用户。
    return raw.slice(0, 200) || fallback;
  }
}

export { API_BASE };

// Pydantic 的校验消息是英文的，措辞也是给开发者看的
// （"String should have at least 8 characters"）。直接展示会在中文界面里突然
// 冒出英文，而且它描述的是"字符串"这种实现细节，不是用户填的那个"密码"。
//
// 按 type 映射成中文——type 是稳定的机器标识，比匹配英文原文可靠。
// 下面这几个值都是对着真实响应确认过的，不是凭印象写的。
const VALIDATION_MESSAGES: Record<string, string> = {
  string_too_short: "密码至少 8 位",
  string_too_long: "密码过长",
  missing: "请把邮箱和密码都填上",
};

// Pydantic 自定义校验器抛出的 ValueError 会被包成 type=value_error，msg 前面
// 加上 "Value error, " 前缀。后端的密码长度校验器写的是中文文案，那份信息比
// 任何通用兜底都准确，所以剥掉前缀原样用。
const VALUE_ERROR_PREFIX = "Value error, ";

function translateValidationItem(item: unknown): string {
  if (!item || typeof item !== "object") return String(item ?? "");
  const { type, msg, loc } = item as { type?: unknown; msg?: unknown; loc?: unknown };
  const text = String(msg ?? "");

  if (typeof type === "string") {
    const mapped = VALIDATION_MESSAGES[type];
    if (mapped) return mapped;
    if (type === "value_error") {
      // 邮箱的 value_error 来自 EmailStr，原文会解释 @ 符号，对用户是噪声。
      if (Array.isArray(loc) && loc.includes("email")) return "邮箱格式不正确";
      // 其余 value_error 是后端自己写的校验器，文案已经是中文的。
      if (text.startsWith(VALUE_ERROR_PREFIX)) return text.slice(VALUE_ERROR_PREFIX.length);
    }
  }
  return text;
}

// FastAPI 的错误体有两种形状，必须分开处理：
//   HTTPException      → { detail: "邮箱已注册" }
//   Pydantic 校验失败  → { detail: [{ loc, msg, type }, ...] }
// 直接把 detail 塞进模板字符串时，第二种会渲染成 "[object Object]"。
// 用户看到这个既不知道错在哪、也不知道该怎么改——而它恰好出现在最需要清晰
// 指引的地方：注册时密码填太短。
export function parseApiError(body: unknown, fallback: string): string {
  if (typeof body === "string" && body.trim()) return body;
  if (!body || typeof body !== "object") return fallback;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    // 去重：邮箱和密码可能同时触发 missing，两条一样的文案叠在一起很难读。
    const messages = [...new Set(detail.map(translateValidationItem).filter(Boolean))];
    if (messages.length) return messages.join("；");
  }
  return fallback;
}

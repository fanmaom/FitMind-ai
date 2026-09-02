/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  // Next 16 的 dev 模式默认只允许启动时那个 hostname（localhost）请求 dev 资源，
  // 其余来源的 chunk 和 /_next/hmr 一律 403 —— 页面 HTML 照常 200，但 JS 全被拦，
  // 表现为白屏。用 127.0.0.1 或局域网 IP（手机上开）访问就会踩到。
  // 生产构建不受此项影响，它只在 next dev 生效。
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};
module.exports = nextConfig;

import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      imports: ['vue', 'vue-router', 'pinia'],
      resolvers: [ElementPlusResolver()],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [ElementPlusResolver()],
      dts: 'src/components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 后端未配置 CORS:开发环境所有 /api 请求经此代理转发到本地后端。
    // 必须锚定为 '^/api/'(前缀匹配会把 /api-keys 这个前端路由也代理走,
    // 导致直接打开或刷新该页时拿到后端的 404 JSON 而不是 SPA)。
    proxy: {
      '^/api/': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})

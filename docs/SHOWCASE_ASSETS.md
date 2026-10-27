# RP1 展示页三维资产与离线构建

2026-10-24。展示模型来自用户指定的本机素材目录 `/home/shn/Desktop/RP1`，源文件只读，未修改。模型表达为 **RP1.3 静态结构示意，非实时姿态；24/28 自由度适配待实机确认**。姿态不会由监控值驱动；温度、告警与诊断仍使用原有只读数据链路。

## 整机 GLB

运行时资源为 `src/rp1_monitor/web/models/rp1-static-structure.glb`，约 **5.12 MiB**、**692,703 个三角面**。将具有相同原始世界坐标的产品白上身与下肢 Web LOD 合并为整机，保留品牌蓝、显示面与金属部件，不以虚构几何替代 RP1。使用进一步减面与 Meshopt 压缩，几何位置和材质来自已有素材。

来源：

| 原素材 | 体积 | SHA-256 |
| --- | ---: | --- |
| `RP1.3-upper-body-static-web-0918/rp1-upper-body-product-white.glb` | 4,325,172 bytes | `5928631dacc6bae19f969b354846437001224b5b2d6f7eb43415efcc5076013b` |
| `RP1.3-product-white-parts-web-0918/rp1-lower-body-product-white.glb` | 5,155,076 bytes | `9d7c02378a35ce9551f73f5892c30dc989e4d1e1b55ed54729dc32afecc4bcc4` |

输出 SHA-256：`150a230e376ccdb08d93ef14e839788e5f556722f144930885ccb46f481e0f16`。机器可读统计与包围盒记录在 `web/models/model-manifest.json`。输出无外部 URI、动画或蒙皮，不需要向互联网请求模型、贴图或解码器。源资产仍属于原提供方；库许可证不授予模型的额外分发权。

模型用 Three.js / GLTFLoader / MeshoptDecoder 加载，独立居中支架避免 CAD 原点影响旋转。柔和蓝白环境光和软接触阴影属于展示布光。初始向左展示 8 秒，不循环；鼠标悬停、键盘聚焦、全局暂停、页面隐藏或离屏时暂停自动运动。默认渲染按需触发，像素比上限 1.5。减少动画设置停止自动旋转，仍支持主动控制。模型加载失败、WebGL 不支持或上下文丢失时显示已有 RP1 静态图，可重试；静态/三维切换始终只显示一种表示。

拖动或左右方向键旋转，滚轮、双指或缩放按钮缩放，Home 或复位按钮恢复正面。手机单指上下手势保留页面滚动，横向拖动旋转；按钮也提供完整替代操作。

## 库与打包

保留原有 FastAPI + HTML/CSS/原生 JavaScript。仅为指定效果增加以下锁定依赖：

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Three.js | 0.180.0 | 真实 GLB 与 OrbitControls |
| Swiper | 12.0.3 | 800 ms 封面流，真实机器人列表，无自动播放或循环 |
| esbuild | 0.25.10 | 生成浏览器离线 ES 模块 |
| glTF Transform core/extensions/functions | 4.5.0 | 构建时合并与优化 GLB |
| meshoptimizer | 0.25.0 | 构建时减面、Meshopt 编码 |

`frontend/package-lock.json` 固定完整依赖与完整性校验。已将 Three.js、Swiper 和 Meshopt 的 MIT 许可证复制到 `web/vendor/`。浏览器使用 `web/vendor/showcase.bundle.js` 和 `web/vendor/swiper-bundle.min.css`；源码 `web/showcase-3d.js` 有构建时裸模块导入，不应直接用 script 加载。运行时无需 Node.js、npm、CDN 或互联网。

首次准备构建环境：

```bash
cd /home/shn/Desktop/rp1-app
npm --prefix frontend ci --ignore-scripts --no-audit --no-fund
npm --prefix frontend run build
```

`build` 不重新生成 GLB。只有需要重建资产且原素材可用时执行：

```bash
RP1_ASSET_SOURCE=/home/shn/Desktop/RP1 npm --prefix frontend run model
npm --prefix frontend run build
```

`build-model.mjs` 只读输入，仅写本项目输出模型与清单。模型已打包，普通部署不依赖源素材路径。

## 页面接入合同

页面引用 `/assets/vendor/showcase.bundle.js`；模块相对该位置请求 `/assets/models/rp1-static-structure.glb`。Python 包数据包含 `web/vendor/*` 与 `web/models/*`。

三维舞台 DOM：`#model-stage`、`#model-canvas-host`、`#model-fallback`、`#model-status`、`#model-progress`，以及 `#model-retry`、`#model-reset`、`#model-left`、`#model-right`、`#model-zoom-in`、`#model-zoom-out`、`#model-static` 控件。控件名称与加载/错误提示由中文页面提供。

轮播使用 `#fleet-coverflow .swiper-wrapper > .swiper-slide[data-robot-id]`，上一台/下一台控件为 `#fleet-prev`、`#fleet-next`，位置为 `#fleet-position`。由真实机器人数据生成卡片后调用 `window.rp1Showcase.updateFleet(selectedId)`，或发送 `rp1:fleet-ready`，`detail.selectedId` 指定当前机器人。主动切换通过现有 `window.rp1SelectRobot(id)` 选择同一机器人，不产生额外业务数据。

全局动效通过 `document` 上的 `rp1:motion` CustomEvent 接入，`detail={paused:boolean,reduced:boolean}`。同时遵循 `prefers-reduced-motion`，减少动画时封面流展平并关闭过渡。Swiper 键盘切换、按钮禁用边界和中文无障碍名称均保留。

import { build } from 'esbuild';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const vendor = path.join(root, 'src/rp1_monitor/web/vendor');
await fs.mkdir(vendor, { recursive: true });
await build({
  entryPoints: [path.join(root, 'src/rp1_monitor/web/showcase-3d.js')],
  outfile: path.join(vendor, 'showcase.bundle.js'),
  bundle: true,
  nodePaths: [path.join(here, 'node_modules')],
  format: 'esm',
  platform: 'browser',
  target: ['es2020'],
  minify: true,
  legalComments: 'linked',
});
await fs.copyFile(path.join(here, 'node_modules/swiper/swiper-bundle.min.css'), path.join(vendor, 'swiper-bundle.min.css'));
await fs.copyFile(path.join(here, 'node_modules/three/LICENSE'), path.join(vendor, 'THREE-LICENSE.txt'));
await fs.copyFile(path.join(here, 'node_modules/swiper/LICENSE'), path.join(vendor, 'SWIPER-LICENSE.txt'));
await fs.copyFile(path.join(here, 'node_modules/meshoptimizer/LICENSE.md'), path.join(vendor, 'MESHOPT-LICENSE.txt'));
console.log('Offline Three.js / Meshopt / Swiper bundle built.');

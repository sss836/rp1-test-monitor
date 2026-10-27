import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { Document, NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { dedup, getBounds, getPrimitiveVertexCount, mergeDocuments, meshopt, prune, simplify, unpartition, weld } from '@gltf-transform/functions';
import { MeshoptDecoder, MeshoptEncoder, MeshoptSimplifier } from 'meshoptimizer';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const sourceRoot = process.env.RP1_ASSET_SOURCE || '/home/shn/Desktop/RP1';
const inputs = [
  path.join(sourceRoot, 'RP1.3-upper-body-static-web-0918/rp1-upper-body-product-white.glb'),
  path.join(sourceRoot, 'RP1.3-product-white-parts-web-0918/rp1-lower-body-product-white.glb'),
];
const output = path.join(root, 'src/rp1_monitor/web/models/rp1-static-structure.glb');
await Promise.all([MeshoptDecoder.ready, MeshoptEncoder.ready, MeshoptSimplifier.ready]);
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({
  'meshopt.decoder': MeshoptDecoder,
  'meshopt.encoder': MeshoptEncoder,
});
const merged = new Document();
const scene = merged.createScene('RP1.3 static structure');
const sources = [];
for (const input of inputs) {
  const source = await io.read(input);
  const sourceScene = source.getRoot().getDefaultScene() || source.getRoot().listScenes()[0];
  const map = mergeDocuments(merged, source);
  for (const node of sourceScene.listChildren()) scene.addChild(map.get(node));
  const bytes = await fs.readFile(input);
  sources.push({ file: path.relative(sourceRoot, input), bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex'), bounds: getBounds(sourceScene) });
}
merged.getRoot().setDefaultScene(scene);
for (const extra of merged.getRoot().listScenes()) if (extra !== scene) extra.dispose();
const stats = () => {
  let triangles = 0;
  let vertices = 0;
  for (const mesh of merged.getRoot().listMeshes()) for (const primitive of mesh.listPrimitives()) {
    triangles += Math.floor(getPrimitiveVertexCount(primitive, 'render') / 3);
    vertices += getPrimitiveVertexCount(primitive, 'upload');
  }
  return { meshes: merged.getRoot().listMeshes().length, triangles, vertices, bounds: getBounds(scene) };
};
const before = stats();
await merged.transform(weld(), dedup(), simplify({ simplifier: MeshoptSimplifier, ratio: 0.35, error: 0.001, lockBorder: false }), prune(), dedup());
const after = stats();
merged.getRoot().getAsset().generator = 'RP1 monitor static showcase; source CAD assets merged and optimized; not telemetry pose';
await merged.transform(meshopt({ encoder: MeshoptEncoder, level: 'high', quantizePosition: 14, quantizeNormal: 10, quantizeTexcoord: 12 }), unpartition(), prune());
await fs.mkdir(path.dirname(output), { recursive: true });
await io.write(output, merged);
const actual = await io.read(output);
const bytes = await fs.readFile(output);
const report = {
  sourceKind: 'user-provided local RP1.3 CAD Web LOD assets',
  meaning: 'RP1.3 静态结构示意，非实时姿态；24/28适配待实机确认',
  sources, before, after, output: path.relative(root, output), bytes: bytes.length,
  sha256: crypto.createHash('sha256').update(bytes).digest('hex'),
  externalURIs: actual.getRoot().listTextures().map(texture => texture.getURI()).filter(Boolean),
  animations: actual.getRoot().listAnimations().length,
  skins: actual.getRoot().listSkins().length,
  bounds: getBounds(actual.getRoot().getDefaultScene()),
};
await fs.writeFile(path.join(root, 'src/rp1_monitor/web/models/model-manifest.json'), JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report, null, 2));

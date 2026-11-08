// Explicit original synthetic assets only. Never walk outputs or the repository.
import {copyFileSync,mkdirSync} from 'node:fs';
for(const name of ['episode.txt','promos.json','promos.csv','demo_manifest.json']) {
  mkdirSync(new URL('../public/demo/',import.meta.url),{recursive:true});
  copyFileSync(new URL('../../demo/'+name,import.meta.url),new URL('../public/demo/'+name,import.meta.url));
}

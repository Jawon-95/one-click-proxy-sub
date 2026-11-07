'use strict';
// Shared by the catalog UI and its Node-based regression checks.
const RegionalEndpoints=Object.freeze({
  key(line){
    let address=String(line).split('#',1)[0].trim().toLowerCase();
    if(address.endsWith(':443'))address=address.slice(0,-4);
    return address.replace(/\.$/,'');
  },
  keys(value){
    return new Set(String(value).split(/[\n,;]+/).map(line=>this.key(line)).filter(Boolean));
  },
  remove(value,address){
    const key=this.key(address),parts=value.split(/([\n,;]+)/),kept=[];
    for(let i=0;i<parts.length;i+=2){
      if(this.key(parts[i])===key)continue;
      if(kept.length)kept.push(parts[i-1]);
      kept.push(parts[i]);
    }
    return kept.join('');
  },
  merge(value,entries){
    const existing=this.keys(value),added=[];
    for(const entry of entries){
      const key=this.key(entry.address);
      if(!existing.has(key)){existing.add(key);added.push(entry.address+'#'+entry.label);}
    }
    // Preserve the user's draft, order, and remarks, including existing duplicates.
    return {value:added.length?value+(value&&!value.endsWith('\n')?'\n':'')+added.join('\n'):value,added:added.length};
  },
});

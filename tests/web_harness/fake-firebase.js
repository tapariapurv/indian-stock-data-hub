// In-memory stand-in for Firebase, for the local harness only.
const store = window.__store = {};
const user = {uid: "u1", email: "test@example.com", getIdToken: async () => "t"};
export const initializeApp = () => ({}); export const getAuth = () => ({}); export class GoogleAuthProvider {}
export const onAuthStateChanged = (_, cb) => { setTimeout(() => cb(user)); }; export const signInWithPopup = async () => {}; export const signOut = async () => {};
export const getFirestore = () => ({});
export const collection = (_, ...p) => ({path: p.join("/")});
export const doc = (c, id) => ({path: c.path, id});
const snap = (c, id) => ({id, ref: {path: c, id}, exists: () => id in (store[c] || {}), data: () => (store[c] || {})[id] && JSON.parse(JSON.stringify(store[c][id]))});
export const getDoc = async r => snap(r.path, r.id);
export const setDoc = async (r, v) => { JSON.stringify(v, (k, x) => { if (x === undefined) throw new Error("undefined in Firestore: " + k); return x; }); (store[r.path] ||= {})[r.id] = v; };
let n = 0; export const addDoc = async (c, v) => { const id = "d" + ++n; await setDoc({path: c.path, id}, v); return {id, path: c.path}; };
export const deleteDoc = async r => { delete (store[r.path] || {})[r.id]; };
export const where = (f, o, v) => ({f, v}); export const query = (c, w) => ({path: c.path, w});
export const getDocs = async q => { const ids = Object.keys(store[q.path] || {}).filter(id => !q.w || store[q.path][id][q.w.f] === q.w.v); return {size: ids.length, docs: ids.map(id => snap(q.path, id))}; };

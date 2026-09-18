import { createContext, useContext } from "react";

export const AuthContext = createContext(null);

export function useAuth() {
  return useContext(AuthContext);
}

export function canAccess(me, priv) {
  if (!priv) return true;
  if (!me) return false;
  if (me.is_owner) return true;
  return (me.privileges || []).includes(priv);
}

export function canDo(me, module, action) {
  if (!me) return false;
  if (me.is_owner) return true;
  return (me.privileges || []).includes(`${module}.${action}`);
}

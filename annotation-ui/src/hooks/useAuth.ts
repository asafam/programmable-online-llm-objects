import { useEffect, useState } from 'react';
import { onAuthStateChanged, signOut } from 'firebase/auth';
import type { User } from 'firebase/auth';
import { auth } from '../firebase';

const ALLOWED_EMAIL = 'asaf.ach@gmail.com';

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    return onAuthStateChanged(auth, (u) => {
      if (u && u.email !== ALLOWED_EMAIL) {
        signOut(auth);
        setUser(null);
      } else {
        setUser(u);
      }
      setLoading(false);
    });
  }, []);

  return { user, loading };
}

import { Navigate, Route, Routes } from "react-router-dom";
import { Center, Loader } from "@mantine/core";
import { AuthProvider, useAuth } from "./hooks/useAuth";
import { LoginScreen } from "./screens/LoginScreen";
import { HomeShell } from "./screens/HomeShell";

function RequireAuth({ children }: { children: JSX.Element }) {
  const { status } = useAuth();

  if (status === "loading") {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  if (status === "unauthenticated") {
    return <Navigate to="/login" replace />;
  }

  return children;
}

function RedirectIfAuthed({ children }: { children: JSX.Element }) {
  const { status } = useAuth();

  if (status === "loading") {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  if (status === "authenticated") {
    return <Navigate to="/" replace />;
  }

  return children;
}

function AppRoutes() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfAuthed>
            <LoginScreen />
          </RedirectIfAuthed>
        }
      />
      <Route
        path="/"
        element={
          <RequireAuth>
            <HomeShell />
          </RequireAuth>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}

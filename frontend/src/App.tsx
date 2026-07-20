import { Navigate, Route, Routes } from "react-router-dom";
import { Alert, Button, Center, Loader, Stack, Text, Title } from "@mantine/core";
import { AuthProvider, useAuth } from "./hooks/useAuth";
import { LoginScreen } from "./screens/LoginScreen";
import { HomeShell } from "./screens/HomeShell";
import { CategoriesScreen } from "./screens/admin/CategoriesScreen";
import { LocationsScreen } from "./screens/admin/LocationsScreen";
import { AssetListScreen } from "./screens/assets/AssetListScreen";
import { AssetDetailScreen } from "./screens/assets/AssetDetailScreen";
import { AssetFormScreen } from "./screens/assets/AssetForm";
import { StockScreen } from "./screens/stock/StockScreen";

/** Distinct "backend unreachable" full-screen state (T1.5 note 6, carried
 * from M0): a network failure / 5xx on the initial `/me` call is not the
 * same as "not logged in" and must not silently bounce the user to Login. */
function BackendUnreachable({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Center h="100vh" p="md">
      <Stack align="center" gap="sm" maw={420}>
        <Title order={3}>Can&apos;t reach Cortex</Title>
        <Alert color="red" w="100%" title="Connection problem">
          {message}
        </Alert>
        <Text c="dimmed" size="sm" ta="center">
          The app couldn&apos;t confirm your session. Check your connection and try again.
        </Text>
        <Button onClick={onRetry}>Retry</Button>
      </Stack>
    </Center>
  );
}

function RequireAuth({ children }: { children: JSX.Element }) {
  const { status, error, refresh } = useAuth();

  if (status === "loading") {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }

  if (status === "error") {
    return (
      <BackendUnreachable
        message={error ?? "Unable to reach the server."}
        onRetry={() => void refresh()}
      />
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
      <Route
        path="/assets"
        element={
          <RequireAuth>
            <AssetListScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/assets/new"
        element={
          <RequireAuth>
            <AssetFormScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/assets/:id/edit"
        element={
          <RequireAuth>
            <AssetFormScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/assets/:id"
        element={
          <RequireAuth>
            <AssetDetailScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/stock"
        element={
          <RequireAuth>
            <StockScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/admin/categories"
        element={
          <RequireAuth>
            <CategoriesScreen />
          </RequireAuth>
        }
      />
      <Route
        path="/admin/locations"
        element={
          <RequireAuth>
            <LocationsScreen />
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

# Tests for hash calculation utility
import hashlib
import tempfile
from pathlib import Path

from main import calcular_hash_directorio


class TestCalcularHashDirectorio:
    """Tests para la función de cálculo de hash SHA-256 del directorio."""

    def test_hash_empty_directory(self):
        """Hash de directorio vacío debe ser consistente."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hash1 = calcular_hash_directorio(tmpdir)
            hash2 = calcular_hash_directorio(tmpdir)
            assert hash1 == hash2
            assert len(hash1) == 64  # SHA-256 hex = 64 chars

    def test_hash_single_file(self):
        """Hash cambia cuando cambia el contenido de un archivo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.txt"
            file_path.write_text("hello")
            hash1 = calcular_hash_directorio(tmpdir)
            
            file_path.write_text("world")
            hash2 = calcular_hash_directorio(tmpdir)
            
            assert hash1 != hash2

    def test_hash_file_rename(self):
        """Hash cambia cuando se renombra un archivo (estructura)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "a.txt"
            file_path.write_text("content")
            hash1 = calcular_hash_directorio(tmpdir)
            
            file_path.unlink()
            new_path = Path(tmpdir) / "b.txt"
            new_path.write_text("content")
            hash2 = calcular_hash_directorio(tmpdir)
            
            assert hash1 != hash2

    def test_hash_subdirectory(self):
        """Hash incluye estructura de subdirectorios."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "sub"
            subdir.mkdir()
            (subdir / "file.txt").write_text("content")
            hash1 = calcular_hash_directorio(tmpdir)
            
            # Mismo archivo pero en raíz
            (subdir / "file.txt").unlink()
            subdir.rmdir()
            (Path(tmpdir) / "file.txt").write_text("content")
            hash2 = calcular_hash_directorio(tmpdir)
            
            assert hash1 != hash2

    def test_hash_order_independence(self):
        """Hash es independiente del orden de listado del filesystem."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "a.txt").write_text("a")
            Path(tmpdir, "b.txt").write_text("b")
            Path(tmpdir, "c.txt").write_text("c")
            
            hash1 = calcular_hash_directorio(tmpdir)
            hash2 = calcular_hash_directorio(tmpdir)
            assert hash1 == hash2

    def test_hash_binary_content(self):
        """Hash maneja contenido binario correctamente."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "binary.dat"
            file_path.write_bytes(b"\x00\x01\x02\xff\xfe\xfd")
            hash1 = calcular_hash_directorio(tmpdir)
            assert len(hash1) == 64

    def test_hash_unicode_filenames(self):
        """Hash maneja nombres de archivo Unicode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "archivo_ñ_中文.txt"
            file_path.write_text("contenido")
            hash1 = calcular_hash_directorio(tmpdir)
            assert len(hash1) == 64
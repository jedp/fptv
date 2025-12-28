import ctypes
from ctypes import CFUNCTYPE, c_void_p, c_char_p

from ctypes.util import find_library


def _load_gl():
    # On Pi/KMS this is often GLESv2; fallback to desktop GL.
    for name in ("GLESv2", "GL"):
        path = find_library(name)
        if path:
            try:
                return ctypes.CDLL(path)
            except OSError:
                pass
    raise RuntimeError("Could not load GLESv2 or GL")


GL_INFO_LOG_LENGTH = 0x8B84

GL = _load_gl()

GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82

GL.glDisable.argtypes = [ctypes.c_uint]
GL.glDisable.restype = None

GL.glViewport.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
GL.glViewport.restype = None

GL.glClearColor.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]
GL.glClearColor.restype = None

GL.glClear.argtypes = [ctypes.c_uint]
GL.glClear.restype = None

GL.glEnable.argtypes = [ctypes.c_uint]
GL.glEnable.restype = None

GL.glBlendFunc.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glBlendFunc.restype = None

GL.glActiveTexture.argtypes = [ctypes.c_uint]
GL.glActiveTexture.restype = None

GL.glGenTextures.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
GL.glGenTextures.restype = None

GL.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glBindTexture.restype = None

GL.glTexParameteri.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
GL.glTexParameteri.restype = None

GL.glTexImage2D.argtypes = [
    ctypes.c_uint, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
]
GL.glTexImage2D.restype = None

GL.glTexSubImage2D.argtypes = [
    ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
]
GL.glTexSubImage2D.restype = None

GL.glDrawArrays.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
GL.glDrawArrays.restype = None

# Shader/program functions
GL.glCreateShader.argtypes = [ctypes.c_uint]
GL.glCreateShader.restype = ctypes.c_uint

GL.glShaderSource.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p),
                              ctypes.POINTER(ctypes.c_int)]
GL.glShaderSource.restype = None

GL.glCompileShader.argtypes = [ctypes.c_uint]
GL.glCompileShader.restype = None

GL.glGetShaderiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
GL.glGetShaderiv.restype = None

GL.glGetShaderInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p]
GL.glGetShaderInfoLog.restype = None

GL.glCreateProgram.argtypes = []
GL.glCreateProgram.restype = ctypes.c_uint

GL.glAttachShader.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glAttachShader.restype = None

GL.glLinkProgram.argtypes = [ctypes.c_uint]
GL.glLinkProgram.restype = None

GL.glGetProgramiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
GL.glGetProgramiv.restype = None

GL.glGetProgramInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p]
GL.glGetProgramInfoLog.restype = None

GL.glUseProgram.argtypes = [ctypes.c_uint]
GL.glUseProgram.restype = None

GL.glGetAttribLocation.argtypes = [ctypes.c_uint, ctypes.c_char_p]
GL.glGetAttribLocation.restype = ctypes.c_int

GL.glGetUniformLocation.argtypes = [ctypes.c_uint, ctypes.c_char_p]
GL.glGetUniformLocation.restype = ctypes.c_int

GL.glUniform1i.argtypes = [ctypes.c_int, ctypes.c_int]
GL.glUniform1i.restype = None

# VBO + vertex attribs
GL.glGenBuffers.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
GL.glGenBuffers.restype = None

GL.glBindBuffer.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glBindBuffer.restype = None

GL.glBufferData.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint]
GL.glBufferData.restype = None

GL.glEnableVertexAttribArray.argtypes = [ctypes.c_uint]
GL.glEnableVertexAttribArray.restype = None

GL.glVertexAttribPointer.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_int,
                                     ctypes.c_void_p]
GL.glVertexAttribPointer.restype = None

mpv_opengl_get_proc_address_fn = CFUNCTYPE(c_void_p, c_void_p, c_char_p)


def compile_shader(src: str, shader_type: int) -> int:
    sh = GL.glCreateShader(shader_type)
    src_b = src.encode("utf-8")
    src_p = ctypes.c_char_p(src_b)
    length = ctypes.c_int(len(src_b))
    GL.glShaderSource(sh, 1, ctypes.byref(src_p), ctypes.byref(length))
    GL.glCompileShader(sh)

    ok = ctypes.c_int(0)
    GL.glGetShaderiv(sh, GL_COMPILE_STATUS, ctypes.byref(ok))
    if not ok.value:
        log_len = ctypes.c_int(0)
        GL.glGetShaderiv(sh, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
        buf = ctypes.create_string_buffer(log_len.value or 4096)
        GL.glGetShaderInfoLog(sh, len(buf), None, buf)
        raise RuntimeError("Shader compile failed:\n" + buf.value.decode("utf-8", "replace"))
    return sh


def link_program(vs: int, fs: int) -> int:
    prog = GL.glCreateProgram()
    GL.glAttachShader(prog, vs)
    GL.glAttachShader(prog, fs)
    GL.glLinkProgram(prog)

    ok = ctypes.c_int(0)
    GL.glGetProgramiv(prog, GL_LINK_STATUS, ctypes.byref(ok))
    if not ok.value:
        log_len = ctypes.c_int(0)
        GL.glGetProgramiv(prog, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
        buf = ctypes.create_string_buffer(max(1, log_len.value))
        GL.glGetProgramInfoLog(prog, len(buf), None, buf)
        raise RuntimeError(f"Program link failed:\n{buf.value.decode('utf-8', 'replace')}")
    return prog

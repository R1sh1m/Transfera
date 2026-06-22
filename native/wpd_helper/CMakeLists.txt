cmake_minimum_required(VERSION 3.15)
project(wpd_helper LANGUAGES CXX)

# Windows-only project
if(NOT WIN32)
    message(FATAL_ERROR "wpd_helper is Windows-only (requires WPD COM API)")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Release build by default
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# Single-file executable
add_executable(wpd_helper wpd_helper.cpp)

# Statically link the CRT for zero runtime dependencies
set_property(TARGET wpd_helper PROPERTY
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# Compiler flags
target_compile_options(wpd_helper PRIVATE
    /W4
    /WX
    /permissive-
    /Zc:wchar_t
    /utf-8
)

# Required libraries
target_link_libraries(wpd_helper PRIVATE
    ole32
    portabledeviceguids
    shlwapi
    propsys
)

# Output to backend/bin/ for easy discovery by the Python backend
set_target_properties(wpd_helper PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../backend/bin"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/../../backend/bin"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG "${CMAKE_CURRENT_SOURCE_DIR}/../../backend/bin_debug"
)

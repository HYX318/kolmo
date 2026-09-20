cmake_minimum_required(VERSION 3.20)
project(kolmo_minute_read LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Reuse the Arrow C++ headers and shared libraries shipped with pyarrow.
# Python is used at configure time only; the executable reads Parquet in C++.
set(KOLMO_PYTHON "${CMAKE_CURRENT_SOURCE_DIR}/../../.venv/bin/python" CACHE FILEPATH
    "Python interpreter with pyarrow installed")
execute_process(
  COMMAND "${KOLMO_PYTHON}" -c "import pyarrow; print(pyarrow.get_include()); print(pyarrow.get_library_dirs()[0])"
  RESULT_VARIABLE ARROW_RESULT OUTPUT_VARIABLE ARROW_PATHS
  OUTPUT_STRIP_TRAILING_WHITESPACE)
if(NOT ARROW_RESULT EQUAL 0)
  message(FATAL_ERROR "Cannot locate pyarrow; set -DKOLMO_PYTHON=/path/to/python")
endif()
string(REPLACE "\n" ";" ARROW_PATHS "${ARROW_PATHS}")
list(GET ARROW_PATHS 0 ARROW_INCLUDE)
list(GET ARROW_PATHS 1 ARROW_LIBDIR)
foreach(NAME arrow parquet)
  if(APPLE)
    file(GLOB LIB_CANDIDATES "${ARROW_LIBDIR}/lib${NAME}.*.dylib")
  else()
    file(GLOB LIB_CANDIDATES "${ARROW_LIBDIR}/lib${NAME}.so.*")
  endif()
  if(NOT LIB_CANDIDATES)
    message(FATAL_ERROR "Cannot find ${NAME} shared library in ${ARROW_LIBDIR}")
  endif()
  list(SORT LIB_CANDIDATES COMPARE NATURAL ORDER DESCENDING)
  list(GET LIB_CANDIDATES 0 ${NAME}_LIBRARY)
endforeach()
add_executable(kolmo_minute_read main.cpp)
target_include_directories(kolmo_minute_read SYSTEM PRIVATE "${ARROW_INCLUDE}")
target_link_libraries(kolmo_minute_read PRIVATE "${parquet_LIBRARY}" "${arrow_LIBRARY}")
set_target_properties(kolmo_minute_read PROPERTIES BUILD_RPATH "${ARROW_LIBDIR}")
target_compile_options(kolmo_minute_read PRIVATE -Wall -Wextra -Wpedantic)

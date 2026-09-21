###########################################################################
#   Standalone "battle only" build of fheroes2.                           #
#                                                                         #
#   This project lives outside the fheroes2 source tree and does not      #
#   modify it. It reuses the upstream engine, third-party and game        #
#   sources as-is, swapping only the entry point: instead of the main     #
#   menu, the resulting binary boots straight into the battle setup       #
#   screen and loops between setup and combat.                            #
###########################################################################

cmake_minimum_required(VERSION 3.24)
cmake_policy(SET CMP0074 NEW)

project(fheroes2_battle LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_EXTENSIONS OFF)

set(FHEROES2_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../fheroes2" CACHE PATH "Path to the fheroes2 source tree")

cmake_path(NORMAL_PATH FHEROES2_ROOT OUTPUT_VARIABLE FHEROES2_ROOT)

if(NOT EXISTS "${FHEROES2_ROOT}/src/fheroes2/game/fheroes2.cpp")
	message(FATAL_ERROR "fheroes2 sources not found under '${FHEROES2_ROOT}'. Pass -DFHEROES2_ROOT=<path> to point at the source tree.")
endif()

#
# Settings the upstream sub-projects (engine, thirdparty) expect from their parent scope
#
set(GNU_CC_WARN_OPTS
	-pedantic -Wall -Wextra -Wcast-align -Wcast-qual -Wdouble-promotion -Wfloat-conversion -Wfloat-equal
	-Wredundant-decls -Wshadow -Wswitch-default -Wundef -Wunused
	)
set(GNU_CXX_WARN_OPTS
	${GNU_CC_WARN_OPTS} -Wctor-dtor-privacy -Wextra-semi -Wmissing-declarations -Wold-style-cast
	-Woverloaded-virtual -Wsuggest-override
	)
set(MSVC_CC_WARN_OPTS /W4)

# SDL_image is only needed for screenshots in PNG format, which this binary does not use.
set(ENABLE_IMAGE OFF)
set(USE_SDL_VERSION SDL2)

list(APPEND CMAKE_MODULE_PATH ${FHEROES2_ROOT}/cmake)

#
# Library & feature detection, mirroring the upstream top-level CMakeLists
#
find_package(${USE_SDL_VERSION} REQUIRED)
find_package(${USE_SDL_VERSION}_mixer REQUIRED)
find_package(ZLIB REQUIRED)
find_package(Threads)

#
# Upstream sub-projects, built into our own binary directory
#
add_subdirectory(${FHEROES2_ROOT}/src/thirdparty ${CMAKE_BINARY_DIR}/thirdparty)
add_subdirectory(${FHEROES2_ROOT}/src/engine ${CMAKE_BINARY_DIR}/engine)

#
# Game sources, minus the upstream entry point which we replace with our own
#
file(GLOB_RECURSE FHEROES2_GAME_SOURCES CONFIGURE_DEPENDS ${FHEROES2_ROOT}/src/fheroes2/*.cpp)
list(REMOVE_ITEM FHEROES2_GAME_SOURCES ${FHEROES2_ROOT}/src/fheroes2/game/fheroes2.cpp)

# Sources we keep a modified copy of under vendor/. The upstream original is dropped from the build
# in favour of ours; see the header of each vendored file for what was changed and why.
set(FHEROES2_VENDORED_SOURCES
	${CMAKE_CURRENT_SOURCE_DIR}/vendor/battle_interface.cpp
	${CMAKE_CURRENT_SOURCE_DIR}/vendor/ai_battle.cpp
	${CMAKE_CURRENT_SOURCE_DIR}/vendor/battle_only.cpp
	${CMAKE_CURRENT_SOURCE_DIR}/vendor/battle_main.cpp
	)

foreach(FHEROES2_VENDORED_SOURCE IN LISTS FHEROES2_VENDORED_SOURCES)
	cmake_path(GET FHEROES2_VENDORED_SOURCE FILENAME FHEROES2_VENDORED_NAME)

	# Guard against the upstream file being moved or renamed: without a matching original, the
	# vendored copy would silently be compiled in addition to it rather than instead of it.
	set(FHEROES2_SUPERSEDED "")
	foreach(FHEROES2_GAME_SOURCE IN LISTS FHEROES2_GAME_SOURCES)
		cmake_path(GET FHEROES2_GAME_SOURCE FILENAME FHEROES2_GAME_SOURCE_NAME)
		if(FHEROES2_GAME_SOURCE_NAME STREQUAL FHEROES2_VENDORED_NAME)
			list(APPEND FHEROES2_SUPERSEDED ${FHEROES2_GAME_SOURCE})
		endif()
	endforeach()

	list(LENGTH FHEROES2_SUPERSEDED FHEROES2_SUPERSEDED_COUNT)
	if(NOT FHEROES2_SUPERSEDED_COUNT EQUAL 1)
		message(FATAL_ERROR "Expected exactly one upstream '${FHEROES2_VENDORED_NAME}' to supersede, found ${FHEROES2_SUPERSEDED_COUNT}. The vendored copy is out of sync with fheroes2.")
	endif()

	list(REMOVE_ITEM FHEROES2_GAME_SOURCES ${FHEROES2_SUPERSEDED})
endforeach()

list(APPEND FHEROES2_GAME_SOURCES ${FHEROES2_VENDORED_SOURCES})

list(LENGTH FHEROES2_GAME_SOURCES FHEROES2_GAME_SOURCE_COUNT)
if(FHEROES2_GAME_SOURCE_COUNT LESS 2)
	message(FATAL_ERROR "Found only ${FHEROES2_GAME_SOURCE_COUNT} game source file(s) under '${FHEROES2_ROOT}/src/fheroes2'.")
endif()

add_compile_options("$<$<COMPILE_LANG_AND_ID:C,AppleClang,Clang,GNU>:${GNU_CC_WARN_OPTS}>")
add_compile_options("$<$<COMPILE_LANG_AND_ID:CXX,AppleClang,Clang,GNU>:${GNU_CXX_WARN_OPTS}>")
add_compile_options("$<$<OR:$<COMPILE_LANG_AND_ID:C,MSVC>,$<COMPILE_LANG_AND_ID:CXX,MSVC>>:${MSVC_CC_WARN_OPTS}>")

file(GLOB BATTLE_SOURCES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)

add_executable(fheroes2-battle ${FHEROES2_GAME_SOURCES} ${BATTLE_SOURCES})

target_compile_definitions(
	fheroes2-battle
	PRIVATE
	# MSVC: suppress deprecation warnings
	$<$<OR:$<COMPILE_LANG_AND_ID:C,MSVC>,$<COMPILE_LANG_AND_ID:CXX,MSVC>>:_CRT_SECURE_NO_WARNINGS>
	$<$<CONFIG:Debug>:WITH_DEBUG>
	)

# The upstream game sources include each other by bare file name, so every module directory has to
# be on the include path, exactly as the upstream build does it.
foreach(FHEROES2_MODULE
	agg ai army audio battle campaign castle dialog editor game gui h2d heroes image kingdom maps
	monster resource spell system world
	)
	target_include_directories(fheroes2-battle PRIVATE ${FHEROES2_ROOT}/src/fheroes2/${FHEROES2_MODULE})
endforeach()

# Our own headers, which the vendored sources include by bare name just like the upstream ones.
target_include_directories(fheroes2-battle PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

target_link_libraries(
	fheroes2-battle
	engine
	${USE_SDL_VERSION}::${USE_SDL_VERSION}main
	)

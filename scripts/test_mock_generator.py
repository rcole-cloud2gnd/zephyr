#!/usr/bin/env python3
# vim: set syntax=python ts=4 :
# Copyright (c) 2022 Nordic Semiconductor ASA
# SPDX-License-Identifier: Apache-2.0
"""
test_generator.py builds a basic build and mocking framework for a standalone module in the Zephyr project.
By running it from the directory where unit tests should be built and identifying the module to unit test,
it will create the boilerplate build artifacts, most FFF mocks and identify the functions available to test.
There are some limitations, in that it will not try all build all config combinations during pre-processing, so it is
likely that some build flavors will be missed and certain mocking functions will need to be added by hand.

It is built primarily for assisting in writing tests for the Bluetooh subsytem, so some tweaks may
be required for other modules.
"""

from multiprocessing import parent_process
import subprocess
import os
import sys
import re
import io
import copy
import logging
#from pycparser import preprocess_file
#import pycparserext.ext_c_parser
from typing import Sequence, Set
from shutil import which
from collections import defaultdict
import tempfile
import argparse
import itertools
#import clang.cindex
import json
from unittest.mock import DEFAULT
import yaml
import pcpp.preprocessor

# requires pyyaml
# requires pcpp

# Add to LD_LIBRARY_PATH (/usr/lib/llvm-XX/lib/)

# @todo Add support for finding library file
#clang.cindex.Config.set_library_file('/usr/lib/llvm-14/lib/libclang-14.so.1')

ZEPHYR_BASE = os.getenv("ZEPHYR_BASE")


# From https://stackoverflow.com/questions/33103684/get-includes-doesnt-find-standard-library-headers
# clang.cindex.TranslationUnit does not have all latest flags
# see: https://clang.llvm.org/doxygen/group__CINDEX__TRANSLATION__UNIT.html#gab1e4965c1ebe8e41d71e90203a723fe9
CXTranslationUnit_None = 0x0
CXTranslationUnit_DetailedPreprocessingRecord = 0x01
CXTranslationUnit_Incomplete = 0x02
CXTranslationUnit_PrecompiledPreamble = 0x04
CXTranslationUnit_CacheCompletionResults = 0x08
CXTranslationUnit_ForSerialization = 0x10
CXTranslationUnit_CXXChainedPCH = 0x20
CXTranslationUnit_SkipFunctionBodies = 0x40
CXTranslationUnit_IncludeBriefCommentsInCodeCompletion = 0x80
CXTranslationUnit_CreatePreambleOnFirstParse = 0x100
CXTranslationUnit_KeepGoing = 0x200
CXTranslationUnit_SingleFileParse = 0x400
CXTranslationUnit_LimitSkipFunctionBodiesToPreamble = 0x800
CXTranslationUnit_IncludeAttributedTypes = 0x1000
CXTranslationUnit_VisitImplicitAttributes = 0x2000
CXTranslationUnit_IgnoreNonErrorsFromIncludedFiles = 0x4000
CXTranslationUnit_RetainExcludedConditionalBlocks = 0x8000


# Paths added during the execution of the script
PATHS_ADDED = []


def parse_arguments(args: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
                                     fromfile_prefix_chars='+')

    #input_group_option = parser.add_mutually_exclusive_group()
    #input_group_option.add_argument()

    parser.add_argument('sources', metavar='source_files', type=str, nargs='+')
    parser.add_argument('-D,--define', dest='defines', type=str, nargs='*', help="Add additional C Preprocessor Defined")
    parser.add_argument('-I,--include', dest='includes', type=str, nargs='*', help="Add additional C Preprocessor Includes")
    parser.add_argument('--output-json', action="store_true", default=False, help="Keep the generated AST json")
    parser.add_argument('--keep-cpp-file', action="store_true", default=False, 
                        help="Keep the generated intermediate preprocessor output.")
    parser.add_argument('--working-dir', type=str, default='./pre_process', 
                        help="Working directory where intermediate file are stored. Will be created if it does not exist.")
    parser.add_argument('--test-root-dir', type=str, default="./", help="Root directory for the output host tests.")
    parser.add_argument('--mocks-output-dir', type=str, default="mocks", help="Directory relative to test-root-dir to store mock functions.")
    parser.add_argument('--regen-mock-main', action="store_true", help="Regenerate the [module]_mock_test_main.c that calls all public functions even if it exists.")
    parser.add_argument('--add-to-git', action="store_true", help="Auto-add newly added file to the current branch.")
    parser.add_argument('--zephyr-base', type=str, help="override ZEPHYR_BASE environment variable.")
    parser.add_argument('--mock-main-includes', type=str, help="File containing the gcc parameters required to build a main.c to identify required mocks.")
    parser.add_argument('--verbose', action="store_true", default=False, help="Output verbose debug")
    # @todo Clean-up prep_process option




    return parser.parse_args(args)


class FuncInstance(object):
    STRIP_EXTENSION_RE = re.compile(r"\.(c|cpp|cxx|h|hpp|i)$", re.IGNORECASE)

    def __init__(self, json_func, location):
        self._raw = json_func
        self._name = json_func['name']
        self._loc = location
        self._included_from = None
        self._return_type = 'void'
        self._params = []
        self._mock_file = "default_mocks"

        # Pick out basic information on the function instance
        if self._loc is not None:
            if 'includedFrom' in self._loc:
                self._included_from = self._loc['includedFrom']['file']
            if 'presumedFile' in self._loc:
                self._mock_file = os.path.basename(self._loc['presumedFile'])

            self._mock_file = FuncInstance.STRIP_EXTENSION_RE.sub('', self._mock_file)
        if 'type' in self._raw and 'qualType' in self._raw['type']:
            self._signature = self._raw['type']['qualType']
            self._decode_signature(self._signature)
        #print(self._raw)

    def __hash__(self):
        return hash(self._name)

    def __eq__(self, other):
        if isinstance(other, FuncInstance):
            return self._name == other._name
        else:
            return self._name == str(other)

    def __repr__(self):
        return f"{self._name} @ {self._loc} from {self._included_from}"

    @property
    def name(self):
        return self._name        

    @property
    def mock_file(self):
        return self._mock_file

    @property
    def location(self):
        return self._loc

    @property
    def included_from(self):
        return self._included_from

    @property
    def signature(self):
        return self._signature

    @property
    def return_type(self):
        return self._return_type

    @property
    def params(self):
        return self._params

    def _decode_signature(self, sig_str: str):
        """
        Convert a simple qualType signature from the clang AST into return type and a list of parameters.
        """
        parenth_depth = 0
        token = ""
        for ch in sig_str:
            if ch == '(':
                if parenth_depth == 0:
                    self._return_type = token.strip()
                    token = ""
                else:
                    token = token + ch
                parenth_depth = parenth_depth + 1
            elif ch == ')':
                if parenth_depth > 0:
                    if parenth_depth > 1:
                        token = token + ch
                    parenth_depth = parenth_depth - 1
                    if parenth_depth == 0:
                        self._params.append(token.strip())
                        token = ""
            elif ch == ',':
                self._params.append(token.strip())
                token = ""
            else:
                token = token + ch




class FuncTable(object):
    def __init__(self):
        self._all_funcs = set()
        self._local_funcs = set()
        self._static_funcs = set()

    @property
    def all_funcs(self):
        return self._all_funcs

    @property
    def local_funcs(self):
        return self._local_funcs

    @property
    def static_funcs(self):
        return self._static_funcs

    def decode_funcs(self, json_decode, filter_funcs=None):                
        """
        Find the relevant FunctionDecl sections in the JSON and group them
        into all-funcs, static-local-funcs and local-funcs with respect
        to the module(s) against which the AST was generated.
        """
        if isinstance(json_decode, dict):
            for attr, val in json_decode.items():
                if attr == 'kind' and val == 'FunctionDecl':      
                    loc = None
                    if 'loc' in json_decode:
                        loc = json_decode['loc']
                    
                    non_static = True
                    if 'storageClass' in json_decode:
                        storage_class = json_decode['storageClass']
                        if storage_class == "static":
                            non_static = False

                    has_inner = 'inner' in json_decode

                    func_instance = FuncInstance(json_decode, loc)
                    self._all_funcs.add(func_instance)

                    if loc:
                        # If there is no filter, or the filter passes
                        if (not filter_funcs or filter_funcs(func_instance)) and has_inner:
                            # Add the function to a table
                            if non_static:
                                self._local_funcs.add(func_instance)
                            else:
                                self._static_funcs.add(func_instance)
                self.decode_funcs(val, filter_funcs)
        elif isinstance(json_decode, list):
            for node in json_decode:
                self.decode_funcs(node, filter_funcs)



class TestFuncHelper(object):
    def __init__(self, options: dict={}):
         self._options = options
         self._ast = None
         self._cpp = which('cpp')
         self._gcc = which('gcc')
         self._clang = which('clang')
         self._temp_preproc_files = []

    def parse(self, source_file: str):
        ftable = FuncTable()

        try:
            includes = [ZEPHYR_BASE + r'/subsys/bluetooth',
                        ZEPHYR_BASE + r'/subsys/bluetooth/host',
                        ZEPHYR_BASE + r'/include',
                        ZEPHYR_BASE + r'/include/zephyr',
                        ZEPHYR_BASE + r'/modules/crypto/tinycrypt/lib/include',
                        ZEPHYR_BASE + r'/zephyr/build/zephyr/include/generated',
                        #ZEPHYR_BASE + r'/zephyr/scripts/test_generator/fake_libc_include'
                        ]

            # Merge known includes with user parameter
            # @note Removing existing includes is current unsupported but can be easily added
            if self._options.includes is not None:
                print(f"Includes {self._options.includes}")
                includes = includes + [inc for inc in self._options.includes]

            cpp_args = []

            # Select the preprocessor:
            cpp_path = self._cpp

            # Fall back on gcc if cpp not found
            if cpp_path is None and self._gcc is not None:
                cpp_path = self._gcc
                cpp_args.append("-E")

            # Fall back on clang if cpp and gcc not found
            if cpp_path is None and self._clang is not None:
                cpp_path = self._clang
                cpp_args.append("--preprocess")

            if cpp_path is None:
                raise RuntimeError("Unable to find C Preprocessor (cpp, gcc or clang)")
            
            # Add include arguments
            cpp_args = cpp_args + ["-I" + i for i in includes]
            cpp_clang_args = []

            # Define various defaults to permit the preprocessed file to be buildable
            cpp_args.append("-DCONFIG_X86")
            cpp_args.append("-DCONFIG_NUM_COOP_PRIORITIES=4")
            cpp_args.append("-DCONFIG_MP_NUM_CPUS=1")
            cpp_args.append("-DCONFIG_SYS_CLOCK_TICKS_PER_SEC=100")
            cpp_args.append("-DCONFIG_LOAPIC_BASE_ADDRESS=0xFEE00000")
            cpp_args.append("-DCONFIG_SYS_CLOCK_HW_CYCLES_PER_SEC=20000000")
            cpp_args.append("-DCONFIG_SYS_CLOCK_MAX_TIMEOUT_DAYS=1")

            # Don't permit __asm__ blocks in __
            #cpp_args.append("-D_ASMLANGUAGE")

            # Hide __attribute__ and __extension__ to avoid compile error (strict C99)
            #cpp_args.append("-D__attribute__(x)=")
            #cpp_args.append("-D__extension__")
            #cpp_args.append("-D__aligned(x)=")
            #cpp_args.append("-D__asm__(...)=")
            #cpp_args.append("-D_must_check=")

            # Define default for Blueototh to permit the preprocessed file to be buildable
            # @todo Prefer to import from the command-line
            cpp_args.append("-DCONFIG_BT_SMP")
            cpp_args.append("-DCONFIG_BT_ID_MAX=2")                               
            cpp_args.append("-DCONFIG_BT_MAX_CONN=2")

            # Args to work around some compiler errors that only occur in clang
            cpp_clang_args.append("-D_Atomic(x)=x")
            cpp_clang_args.append("-D__packed=")
            cpp_clang_args.append("-D_Static_assert(...)=")

            if self._options.defines is not None:
                for d in self._options.defines:
                    cpp_args.append(f"-D{d}")


            # @todo Move pre-processor output and path to argument
            if not os.path.exists(self._options.working_dir):
                os.makedirs(self._options.working_dir)

            source_base_name = os.path.basename(source_file)

            temp_stripped_file_name = ""

            # Create file for stripped source output
            with tempfile.NamedTemporaryFile(mode="w", dir=self._options.working_dir, prefix=f"temp_stripped_module_{source_base_name}_", suffix=".c", delete=False) as tempf:
                temp_stripped_file_name = tempf.name
            
            stripped_source_file = source_file

            if self._options.verbose:
                print(f"Stripping out conditional expressions for function-name pass:\n    IN={source_file} OUT={stripped_source_file}")

            # Strip conditional out of the main source file (to capture all declared functions) and write to temporary
            with open(source_file, "r") as inf:
                with open(temp_stripped_file_name, "w") as outf:
                    ConfigOptionParser.strip_conditionals(inf, outf)
                    stripped_source_file = outf.name

            if self._options.verbose:
                print(f"Preprocessing source file and stripped source file")

            # Preprocess the stripped source file
            normal_pre_proc = self._preprocess_file(cpp_path, source_file, cpp_args=cpp_args)
            stripped_pre_proc = self._preprocess_file(cpp_path, stripped_source_file, cpp_args=cpp_args + cpp_clang_args)

            # @todo Tack function identity onto prefix
            with tempfile.NamedTemporaryFile(mode="w", dir=self._options.working_dir, prefix=f"temp_stripped_pre_proc_{source_base_name}", suffix=".i", delete=False) as tf:                
                print(f"Output file name: {tf.name}")
                if self._options.verbose:
                    print(f"Outputting preprocessor for function-name pass\n    {tf.name}")
                
                print(stripped_pre_proc, file=tf)

                temp_file_name = tf.name

                # We would prefer to use the cindex directly, but the file names from
                # linemarkers don't appear to be preserved, so we'll go directly to
                # the json output where they are preserved.
                if self._options.verbose:
                    logging.debug(f"Building JSON AST: IN={tf.name}")

                json_decode = self._create_ast(tf.name)
                
                if self._options.output_json:
                    json_file_name = temp_file_name + ".json"
                    logging.warning(f"Outputting JSON to {json_file_name}. This may take a moment.")
                    with open(json_file_name, "w") as json_debug:
                        print(json.dumps(json_decode, indent=4), file=json_debug)

                # Use increased recursion-limit if the function depth is too great
                #sys.setrecursionlimit(10000)

                def match_func(func_instance):
                    # If there is no included_from, we should be in the main module
                    return func_instance.included_from is None

                # Decode functions 
                if self._options.verbose:
                    logging.debug(f"Decode function declarations")
                ftable.decode_funcs(json_decode, match_func)

            with tempfile.NamedTemporaryFile(mode="w", dir=self._options.working_dir, prefix=f"temp_normal_pre_proc_{source_base_name}", suffix=".i", delete=False) as tnf:                
                print(normal_pre_proc, file=tnf)
                self._temp_preproc_files.append(tnf.name)


            #parser = pycparserext.ext_c_parser.GnuCParser()
            #self._ast = parser.parse(pre_proc, self._source_file)

            #self._ast = parse_file(self._source_file, use_cpp=True, cpp_args=cpp_args)
        except Exception as ex:
            logging.exception(ex)
        finally:
            return ftable

    def generate_build_artifacts(self, source_file: str, ftable: FuncTable):
        # Create the build-directories for each public function
        self._create_build_dirs(self._options.test_root_dir, ftable.local_funcs)

        # Populate the build-directories with artifacts
        self._populate_build_dirs(source_file, self._options.test_root_dir, ftable.local_funcs)

        # Create a main() to call all the known public interfaces
        main_file = self._create_public_interface_main(source_file, ftable.local_funcs)

        with open(main_file, "r") as mainf:
            main_file_lines = mainf.readlines()

        # Build with main() to identify the functions that are called but not present
        for preproc_file in self._temp_preproc_files:
            # Append the contents of the main_file onto each preprocessor file
            with open(preproc_file, "a") as preprocf:
                for line in main_file_lines:
                    print(line, file=preprocf)
            funcs = self._linker_results(preproc_file)
            print(funcs)
            if len(funcs) > 0:
                self._create_mocks(funcs, ftable)
            else:
                print("WARNING: No functions found requiring mock-ups.")


    def _create_mocks(self, funcs: Set[str], ftable: FuncTable):
        # Make dictionary of all functions
        if not os.path.exists(self._options.mocks_output_dir):
            os.makedirs(self._options.mocks_output_dir)

        all_funcs = {f.name: f for f in ftable._all_funcs}
        for f in funcs:
            if f in all_funcs:
                print(all_funcs[f].mock_file)
            else:
                print(f"Warning: Cannot find declaration for mocked-up function {f}")

    def _create_public_interface_main(self, source_file: str, funcs: Set[FuncInstance]) -> str:
        """
        Generate a public_interface_main_xx.c file
        """
        source_base_name = os.path.basename(source_file)
        public_interface_main = os.path.join(self._options.working_dir, f"public_interface_main_{source_base_name}.c")

        # If we are supposed to regenerate the mocking calls file or it does not exists, create it.
        if self._options.regen_mock_main or not os.path.exists(public_interface_main):
            # Create the mock calls in a fixed location so we can check for it.
            with open(public_interface_main, mode="w") as tf:

                # Output calls to all public interfaces in the module
                print(f"// Temporary Public Main for {source_base_name}", file=tf)
                print(file=tf)
                print(  "int main(void) {", file=tf)

                temp_index = 1
                by_func_name = {}
                # Output temporaries
                for f in funcs:
                    param_temps = []
                    for p in f.params:
                        if p != 'void':
                            temp_name = f"temp_{temp_index}"
                            param_temps.append(temp_name)
                            print(f"    {p} {temp_name};", file=tf)
                            temp_index = temp_index + 1

                    by_func_name[f.name] = param_temps

                print(file=tf)

                # Output function calls with temporary params
                for f in funcs:
                    print(f"    {f.name}(", end='', file=tf)
                    print(', '.join(by_func_name[f.name]), end='', file=tf)
                    print(");", file=tf)
                print(file=tf)
                print(  "    return 0;", file=tf)
                print(  "}", file=tf)

                return tf.name
        elif os.path.exists(public_interface_main):
            logging.warning(f"Warning: {public_interface_main} exists and was not overwritten.")
            logging.warning("Use --regen-mock-main to force regeneration.")
            return public_interface_main
        else:
            return None        


    def _func_dir_name(self, func: FuncInstance) -> str:
        return os.path.join(self._options.test_root_dir, func.name)

    def _create_build_dirs(self, root_path: str, local_funcs: Set[str]):
        """
        Create the directories for each of the CMakeList.txt and parameters for each
        public function passed-in.
        """
        for func in local_funcs:
            dir_name = self._func_dir_name(func)
            if not os.path.exists(dir_name):
                os.makedirs(dir_name)


    def _populate_build_dirs(self, source_file: str, root_path: str, local_funcs: Set[str]):
        for func in local_funcs:
            dir_name = self._func_dir_name(func)
            if os.path.exists(dir_name):
                # Generate CMakeList.txt if it does not exist
                cmake_file = os.path.join(dir_name, "CMakeList.txt")
                if not os.path.exists(cmake_file):
                    # @todo
                    pass

                # @todo Generate prj.config from template (local or passed-in)
                proj_file = os.path.join(dir_name, "prj.config")
                if os.path.exists(cmake_file):
                    # @todo Read existing project file
                    pass
                else:
                    # @todo Populate with the project-config template
                    pass
                
                # @todo Add/update discovered configuration options
                
                # Generate placeholder testcase.yaml
                testcase_file = os.path.join(dir_name, "testcase.yaml")
                if os.path.exists(testcase_file):
                    try:
                        with open(testcase_file, "r") as tcf:
                            yaml_template = yaml.safe_load(tcf)
                    except Exception as ex:
                        logging.exception(ex)
                else:
                    yaml_template = {
                        'tests': []
                    }

                # Ensure the yaml_template (or loaded yaml) has the required sections
                base_name = os.path.basename(source_file)
                if 'common' not in yaml_template:
                    yaml_template['common'] = {'tags': f"test_framework bluetooth host testing {base_name}"}

                if 'tests' not in yaml_template:
                    yaml_template['tests'] = []

                # Populate/update the default test scenarios
                add_funcs = set()
                for t in yaml_template['tests']:
                    pass

                # Rewrite the testcase.yaml
                with open(testcase_file, "w") as tcf:
                    yaml.dump(yaml_template, tcf, default_flow_style=False, allow_unicode=True)


    def _create_ast(self, input_file: str, args: list=[]):        
        if self._clang is None:
            raise RuntimeError("Unable to find clang")

        # [self._clang] +["-Xclang", "-ast-dump=json", "-fsyntax-only", "-fno-color-diagnostics"] + [input_file]
        args = [self._clang] +["-Xclang", "-ast-dump=json", "-fsyntax-only", "-ferror-limit=65536"] + args + [input_file]
        try:
            sub_out = subprocess.run(args, check=False, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            #error_raw = sub_out.stderr
            return json.loads(sub_out.stdout)            

        except OSError as e:
            raise RuntimeError(f"Failed to invoke clang: {e}")
        #json.loads(raw_str)


    UNDEFINED_REFERENCE_RE = re.compile(r"undefined reference to `(.+?)'")

    def _linker_results(self, input_file: str, args: list=[]):        
        if self._options.verbose:
            logging.debug(f"Building output {input_file}")
        
        if self._gcc is None:
            raise RuntimeError("Unable to find gcc")
    
        print(f"Compiling GCC input: {input_file}")

        args = [self._gcc] +["-DBUILD_PREPROC_MOCK_MAIN", "-fmax-errors=0"] + args + [input_file]
        try:
            sub_out = subprocess.run(args, check=False, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            error_raw = sub_out.stderr
            
            #if self._options.verbose:
                #print(error_raw)

            # Identify the names of undefined functions
            funcs = set()
            for line in error_raw.splitlines():
                match = TestFuncHelper.UNDEFINED_REFERENCE_RE.search(line)
                if match:
                    funcs.add(match[1])

            # If there were no undefined functions, show all errors.
            if len(funcs) == 0:
                print(error_raw)

            return funcs

        except OSError as e:
            raise RuntimeError(f"Failed to invoke clang: {e}")

    # def _create_index(self, input_file: str, args: list=[]):        
    #     if self._clang is None:
    #         raise RuntimeError("Unable to find clang")
    #     args = ["-c"] + args
    #     try:
    #         index = clang.cindex.Index.create()
    #         trans_unit = index.parse(input_file, args, options=CXTranslationUnit_DetailedPreprocessingRecord | CXTranslationUnit_KeepGoing)

    #         for node in trans_unit.cursor.walk_preorder():
    #             node_def = node.get_definition()
    #             if node.location.file is None:
    #                 continue
    #             #print(f"NODE: file{node.location.file.name}")
    #             if node.kind.is_declaration():
    #                 print(node.kind, node.spelling, node.location)                    

    #     except OSError as e:
    #         raise RuntimeError(f"Failed to invoke preprocessor: {e}")
                

    def _preprocess_file(self, cpp_path: str, source_file: str, cpp_args: list=[]) -> str:
        args = [cpp_path] + cpp_args + [source_file]
        try:
            return subprocess.check_output(args, universal_newlines=True)
        except OSError as e:
            raise RuntimeError(f"Failed to invoke preprocessor: {e}")



class ConfigOptionParser(object):

    CONFIG_REG = re.compile(r'CONFIG_\w+')
    IFDEF_REG = re.compile(r'#if|#ifdef')
    ELIF_REG = re.compile(r'#elif')
    ELSE_REG = re.compile(r'#else')
    ENDIF_REG = re.compile(r'#endif')
    REMOVE_COMMENTS = re.compile(r'//.*?\n|/\*.*?\*/', flags=re.S)
    FOUND_REG = re.compile(r'found\(1\);', re.MULTILINE)

    def __init__(self):
        self._nested_configs = []
        self._all_inner_configs = defaultdict(list)
        self._conditional_lines = []

    def dump(self):
        print("INNER CONFIGS:")
        for key, val in self._all_inner_configs.items():
            print(key, val)
        print()
        print("NESTED CONFIGS:")
        for config in self._nested_configs:
            print(config)

    @property
    def conditional_lines(self):
        return self._conditional_lines

    @staticmethod
    def strip_conditionals(inf, outf):
        """
        Strips preprocessor conditional pragmatics from an input file and outputs it to an output file.
        Conditionals are replaced with empty lines.
        WARNING: This implementation doesn't currently support line-continuation for conditional expressions.
        """
        for line in inf.readlines():
            line = ConfigOptionParser.REMOVE_COMMENTS.sub('', line)

            matches_ifdef      = ConfigOptionParser.IFDEF_REG.match(line)
            matches_elif       = ConfigOptionParser.ELIF_REG.match(line)
            matches_else       = ConfigOptionParser.ELSE_REG.match(line)
            matches_endif      = ConfigOptionParser.ENDIF_REG.match(line)

            if matches_ifdef or matches_endif or matches_elif or matches_else:
                line = "\n"
            
            outf.write(line)


    def parse_conditional(self, line: str, match_vars: list):
        """
        Identify all combinations of flags in a conditional expression and
        determine what flags satisfy the expression.
        """
        # Build the set of all flags
        flag_sets = []
        for i in range(0, 2**len(match_vars)):
            mask = []
            for pos in range(0, len(match_vars)):
                if (i & (0x1 << pos)) != 0x0:
                    mask.append(f"{match_vars[pos]} 1")
            flag_sets.append(mask)

        return self._find_satisfying_flags(line, flag_sets)
        

    def _find_satisfying_flags(self, line:str, flag_sets: list):
        """
        Determine which sets of flags satisfy (make true) the conditional expression
        """
        satisfies = []
        prepos_expr = f"{line}\nfound(1);\n#endif\n"

        # Run the local preprocessor for each combination to identify which of
        # the flag sets satisfy the conditional expression
        for flag_set in flag_sets:
            pp = pcpp.preprocessor.Preprocessor()
            for flag in flag_set:
                pp.define(flag)

            pp.parse(prepos_expr)
            with io.StringIO() as outf:
                pp.write(outf)
                outf.seek(0)
                str_out = outf.read()
                if ConfigOptionParser.FOUND_REG.search(str_out):
                    satisfies.append([f.split()[0] for f in flag_set])
        return satisfies
        

    def find_config_masks(self, s: str):
        with open(s, "r") as input:
            line_number = 1
            nesting_depth = 0
            nesting_warned = False

            current_conditions = []
            current_configs = {}

            for line in input.readlines():
                # Adapted from https://stackoverflow.com/questions/241327/remove-c-and-c-comments-using-python
                # Strip C/C++ comments
                line = ConfigOptionParser.REMOVE_COMMENTS.sub('', line)

                matches_config     = ConfigOptionParser.CONFIG_REG.findall(line)
                matches_ifdef      = ConfigOptionParser.IFDEF_REG.match(line)
                matches_elif       = ConfigOptionParser.ELIF_REG.match(line)
                matches_else       = ConfigOptionParser.ELSE_REG.match(line)
                matches_endif      = ConfigOptionParser.ENDIF_REG.match(line)

                # Include any CONFIG_X in the list of all inner configs
                for match in matches_config:
                    self._all_inner_configs[match].append(line_number)
                    
                # If this is a conditional, process CONFIG_X
                if matches_ifdef or matches_endif or matches_elif or matches_else:
                    self._conditional_lines.append(line_number)

                    # On if or ifdef, push the set of action conditions and increase nesting
                    if matches_ifdef:
                        nesting_depth = nesting_depth + 1
                        current_conditions.append( {'depth': nesting_depth, 'line': line_number, 'segments': []} )

                        # Parse condtional expression
                        satisying_configs = self.parse_conditional(line, matches_config)

                        current_configs['start'] = line_number
                        current_configs['options'] = matches_config
                        current_configs['satisfy'] = satisying_configs
                        

                    elif matches_endif:                       
                        current_configs['end'] = line_number 
                        current_conditions[-1]['segments'].append(copy.deepcopy(current_configs))

                        nesting_depth = nesting_depth - 1

                    elif matches_elif:
                        current_configs['end'] = line_number 
                        current_conditions[-1]['segments'].append(copy.deepcopy(current_configs))
                        current_conditions.append( {'depth': nesting_depth, 'line': line_number, 'segments': []} )

                        # Parse condtional expression
                        satisying_configs = self.parse_conditional(line, matches_config)

                        current_configs['start'] = line_number + 1
                        current_configs['options'] = matches_config
                        current_configs['satisfy'] = satisying_configs

                    elif matches_else:
                        current_configs['end'] = line_number 
                        current_conditions[-1]['segments'].append(copy.deepcopy(current_configs))
                        current_conditions.append( {'depth': nesting_depth, 'line': line_number, 'segments': []} )

                        current_configs['start'] = line_number + 1

                        #current_configs['satisfy'] = satisying_configs
                        # @todo push the opposite states for the conditionals
                        pass                    

                    
                    
                    if nesting_depth < 0:
                        if not nesting_warned:
                            logging.warning("conditional nesting invalid. Too many #endifs")
                            nesting_warned = True

                    #print(line_number, line)
                    if matches_config:

                        for match in matches_config:
                            #print(nesting_depth, line_number, match)
                            pass
                
                line_number = line_number + 1

            if nesting_depth != 0:
                logging.warning("conditional nesting invalid. Too few #endifs")

            #print(current_conditions)
            #json.dump(current_conditions, sys.stdout, indent=4)
    

def print_func_table(title: str, func_list: Set[str], out=sys.stdout):
    """
    Pretty-print a table of functions from the AST parser (see class FuncInstance).
    """
    print(f"{title}:", file=out)

    if len(func_list) > 0:
        max_func_len = max([len(i.name) for i in func_list])
        max_ret_len  = max([len(i.return_type) for i in func_list])
    else:
        max_func_len = 0    
        max_ret_len  = 0
    for f in func_list:
        name = f.name + " "*(max_func_len - len(f.name))
        ret = " "*(max_ret_len - len(f.return_type)) + f.return_type

        name_ret_param_len = len(f"    {name} -- {ret} [")
        if len(f.params) > 0:
            print(f"    {name} -- {ret} [{f.params[0]}", file=out, end='')
            if len(f.params) == 1:
                print("]", file=out)
            else:
                print(",", file=out)
        else:
            print(f"    {name} -- {ret} []", file=out)

        index = 1
        for p in f.params[1:]:
            cont_line = " "*name_ret_param_len
            if index + 1 < len(f.params):
                cont_line = cont_line + p + ","
            else:
                cont_line = cont_line + p + "]"
            print(cont_line, file=out)
            index = index + 1


def main() -> int:
    options = parse_arguments(sys.argv[1:])
    if options.zephyr_base:
        ZEPHYR_BASE = options.zephyr_base
    for s in options.sources:
        print(f"Source {s}")
        if os.path.exists(s):        
            option_parser = ConfigOptionParser()  
            option_parser.find_config_masks(s)            

            #option_parser.dump()

            func_parser = TestFuncHelper(options)
            funcs = func_parser.parse(s)
            
            func_parser.generate_build_artifacts(s, funcs)

            if options.verbose:
                #print_func_table("Other FUNCS", funcs.all_funcs - funcs.static_funcs - funcs.local_funcs)
                print_func_table("Static Local FUNCS", funcs.static_funcs)
                print_func_table("Local FUNCS", funcs.local_funcs)


            if options.add_to_git:
                if options.verbose:
                    print("Adding modified/added files to current git branch:")
                for path in PATHS_ADDED:
                    print(f"TODO: Add to git: {path}")
                
        else:
            logging.error(f"Unable to find source_file: {s}")
    return 0



if __name__ == "__main__":
    ret = 0
    try:
        ret = main()
    finally:
        if (os.name != "nt") and os.isatty(1):
            # (OS is not Windows) and (stdout is interactive)
            # Correct any terminal glitches that may have occurred from
            # outputting escape sequences during script execution.
            os.system("stty sane <&1")

    sys.exit(ret)



